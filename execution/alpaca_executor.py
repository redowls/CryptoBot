"""Alpaca order submission + position reconciliation.

Per initial.md §6 Session 7, this module:
- Wraps TradingClient(paper=...) and exposes submit_market_order /
  cancel_all_open_orders / get_position / reconcile_positions.
- Owns client_order_id generation in the format the build plan locks down:
  `{SYMBOL_NO_SLASH}-{UTC_YYYYMMDDHHMMSS}-{uuid6}`. This is the idempotency key
  — every order is persisted to `orders` BEFORE submission so a crash mid-call
  still leaves an auditable row (CLAUDE.md "Order idempotency" invariant).
- Treats Alpaca as authoritative for positions: reconcile_positions diffs
  Alpaca's /v2/positions against the SQL `positions` table and writes the
  difference. `current_stop` (which Alpaca doesn't know) is preserved through
  the MERGE in Repository.upsert_position via COALESCE.

Risk-aware position sizing and the daily/drawdown caps land in Session 8
(risk/manager.py). This module is a thin broker adapter — it does not decide
whether a trade *should* fire, only how to fire it cleanly.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from db.repository import Repository

log = logging.getLogger(__name__)


@dataclass
class SubmittedOrder:
    """Lightweight view of a submitted Alpaca order, shape-stable for callers
    (the alpaca-py Order model has changed field names across versions)."""
    client_order_id: str
    alpaca_order_id: str | None
    symbol: str
    side: str
    qty: float
    status: str
    raw: dict


class Executor:
    def __init__(
        self,
        repo: Repository | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool = True,
        trading_client: TradingClient | None = None,
    ) -> None:
        self.repo = repo or Repository()
        self.paper = paper
        if trading_client is not None:
            self._trading = trading_client
        else:
            import os
            self._trading = TradingClient(
                api_key=api_key or os.getenv("ALPACA_API_KEY"),
                secret_key=secret_key or os.getenv("ALPACA_SECRET_KEY"),
                paper=paper,
            )

    # ---------- order submission ----------

    @staticmethod
    def make_client_order_id(symbol: str) -> str:
        """`{SYMBOL_NO_SLASH}-{UTC_YYYYMMDDHHMMSS}-{uuid6}` — locked by initial.md."""
        sym = symbol.replace("/", "")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{sym}-{ts}-{uuid.uuid4().hex[:6]}"

    def submit_market_order(
        self,
        symbol: str,
        qty: float | Decimal,
        side: str,
        client_order_id: str | None = None,
        signal_id: int | None = None,
    ) -> SubmittedOrder:
        """Submit a crypto market order. Persists to `orders` BEFORE submission.

        Two writes happen:
        1. INSERT orders(status='pending', client_order_id=...) — pre-submit row
           so an audit trail exists even if the API call crashes / times out.
        2. UPDATE orders SET alpaca_order_id, status, raw_response after the
           API responds. If submission raises, the row stays 'pending' — that's
           the signal that this client_order_id never reached Alpaca.
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
        coid = client_order_id or self.make_client_order_id(symbol)

        self.repo.insert_order(
            client_order_id=coid,
            symbol=symbol,
            side=side,
            type="market",
            qty=qty,
            status="pending",
            signal_id=signal_id,
        )

        request = MarketOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,  # crypto supports gtc/ioc only
            client_order_id=coid,
        )

        try:
            order = self._trading.submit_order(order_data=request)
        except Exception as e:
            log.exception("submit_market_order failed for %s %s qty=%s", side, symbol, qty)
            self.repo.update_order_status(
                client_order_id=coid,
                status="error",
                raw_response=f"{type(e).__name__}: {e}",
            )
            raise

        raw = _model_dump(order)
        alpaca_id = str(raw.get("id")) if raw.get("id") is not None else None
        # raw.get('status') may still be an Enum if model_dump returned the v1
        # dict-form; coerce to its string value either way.
        status = str(_enum_value(raw.get("status", "new")))
        filled_qty = raw.get("filled_qty")
        filled_avg = raw.get("filled_avg_price")
        filled_at = raw.get("filled_at")

        self.repo.update_order_status(
            client_order_id=coid,
            status=status,
            alpaca_order_id=alpaca_id,
            filled_qty=_nonzero(filled_qty),
            filled_avg_price=_nonzero(filled_avg),
            filled_at=_parse_dt(filled_at),
            raw_response=json.dumps(raw, default=str)[:8000],
        )

        return SubmittedOrder(
            client_order_id=coid,
            alpaca_order_id=alpaca_id,
            symbol=symbol,
            side=side,
            qty=float(qty),
            status=status,
            raw=raw,
        )

    def close_position(
        self,
        symbol: str,
        qty_hint: float | Decimal,
        client_order_id: str | None = None,
        signal_id: int | None = None,
    ) -> SubmittedOrder:
        """Close the full open position via TradingClient.close_position(symbol).

        Why this exists instead of `submit_market_order(side='SELL', qty=db_qty)`:
        our `positions.qty` is `DECIMAL(28,8)` but Alpaca tracks crypto holdings
        to 9+ decimal places. Reconcile rounds Alpaca's value into 8dp; a later
        SELL using the rounded qty exceeds the true holding by a few satoshis
        and Alpaca rejects with HTTP 403 + code 40310000 "insufficient balance".
        `close_position(symbol)` delegates the qty decision to Alpaca's own
        balance, sidestepping the round-trip through our DECIMAL column.

        `qty_hint` is recorded on the pre-submit `orders` row for audit (the
        column is NOT NULL); the actual filled qty lands in `filled_qty` from
        Alpaca's response.
        """
        coid = client_order_id or self.make_client_order_id(symbol)

        self.repo.insert_order(
            client_order_id=coid,
            symbol=symbol,
            side="SELL",
            type="market",
            qty=qty_hint,
            status="pending",
            signal_id=signal_id,
        )

        try:
            order = self._trading.close_position(symbol)
        except Exception as e:
            log.exception("close_position failed for %s", symbol)
            self.repo.update_order_status(
                client_order_id=coid,
                status="error",
                raw_response=f"{type(e).__name__}: {e}",
            )
            raise

        raw = _model_dump(order)
        alpaca_id = str(raw.get("id")) if raw.get("id") is not None else None
        status = str(_enum_value(raw.get("status", "new")))
        filled_qty = raw.get("filled_qty")
        filled_avg = raw.get("filled_avg_price")
        filled_at = raw.get("filled_at")

        self.repo.update_order_status(
            client_order_id=coid,
            status=status,
            alpaca_order_id=alpaca_id,
            filled_qty=_nonzero(filled_qty),
            filled_avg_price=_nonzero(filled_avg),
            filled_at=_parse_dt(filled_at),
            raw_response=json.dumps(raw, default=str)[:8000],
        )

        return SubmittedOrder(
            client_order_id=coid,
            alpaca_order_id=alpaca_id,
            symbol=symbol,
            side="SELL",
            qty=float(qty_hint),
            status=status,
            raw=raw,
        )

    def cancel_all_open_orders(self, symbol: str) -> int:
        """Cancel every open order for `symbol`. Returns the count cancelled."""
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        try:
            open_orders = self._trading.get_orders(filter=req)
        except TypeError:
            # older alpaca-py used a positional/keyword named differently
            open_orders = self._trading.get_orders(req)
        count = 0
        for o in open_orders:
            data = _model_dump(o)
            order_id = data.get("id")
            if order_id is None:
                continue
            self._trading.cancel_order_by_id(order_id)
            self.repo.update_order_status(
                client_order_id=str(data.get("client_order_id", "")),
                status="canceled",
            )
            count += 1
        return count

    # ---------- position read / reconcile ----------

    def get_position(self, symbol: str) -> dict | None:
        """Return Alpaca's view of the position for `symbol`, or None if flat.

        alpaca-py raises (404) when there's no position — we translate that to
        None rather than letting it bubble, because "no position" is the common
        case and not exceptional.
        """
        try:
            pos = self._trading.get_open_position(symbol)
        except Exception:
            return None
        return _model_dump(pos)

    def reconcile_positions(self) -> dict[str, int]:
        """Diff Alpaca /v2/positions against SQL `positions`. Alpaca wins.

        - Alpaca has it, SQL doesn't  → upsert (current_stop=None; risk/sizing
          can later attach a stop when it sees the new row).
        - SQL has it, Alpaca doesn't  → delete (we're flat per Alpaca).
        - Both have it                → upsert with current qty/avg_entry;
          current_stop is preserved by the MERGE's COALESCE on NULL.
        """
        alpaca_positions = self._trading.get_all_positions()
        alpaca_by_symbol: dict[str, dict] = {}
        for p in alpaca_positions:
            data = _model_dump(p)
            sym = _normalize_crypto_symbol(str(data.get("symbol", "")))
            alpaca_by_symbol[sym] = data

        sql_df = self.repo.db.query_df("SELECT symbol FROM positions;")
        sql_symbols = set(sql_df["symbol"].tolist()) if not sql_df.empty else set()

        added = updated = removed = 0
        for sym, data in alpaca_by_symbol.items():
            qty = abs(float(data.get("qty", 0)))
            avg = float(data.get("avg_entry_price", 0))
            self.repo.upsert_position(
                symbol=sym, qty=qty, avg_entry_price=avg,
                current_stop=None,  # MERGE COALESCEs to preserve existing
            )
            if sym in sql_symbols:
                updated += 1
            else:
                added += 1

        for sym in sql_symbols - set(alpaca_by_symbol):
            self.repo.delete_position(sym)
            removed += 1

        return {"added": added, "updated": updated, "removed": removed}


# ---------- helpers ----------

def _model_dump(obj: Any) -> dict:
    """Best-effort flatten of an alpaca-py Pydantic model to a plain dict.

    Pydantic v2 uses .model_dump(); v1 used .dict(). Some alpaca-py releases
    return raw dicts already. Handle all three without locking to a version.

    `mode='json'` matters: it converts Enum values to their string `.value`
    (e.g. OrderStatus.NEW -> 'new') and datetimes to ISO strings, so callers
    get DB-safe primitives instead of enum reprs that overflow VARCHAR columns.
    """
    if isinstance(obj, dict):
        return obj
    fn = getattr(obj, "model_dump", None)
    if callable(fn):
        try:
            return fn(mode="json")
        except TypeError:
            return fn()
        except Exception:
            pass
    fn = getattr(obj, "dict", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            pass
    return {k: getattr(obj, k) for k in vars(obj)} if hasattr(obj, "__dict__") else {}


def _enum_value(v: Any) -> Any:
    """Pull .value off an Enum; pass scalars through unchanged."""
    return getattr(v, "value", v)


def _normalize_crypto_symbol(s: str) -> str:
    """Alpaca historically returned crypto symbols as 'BTCUSD'; modern alpaca-py
    returns 'BTC/USD'. Normalize to slashed form so SQL keys stay consistent."""
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "BTC"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return s


def _nonzero(v: Any) -> float | None:
    """Coerce filled_qty/filled_avg_price for the COALESCE pattern — Alpaca
    reports '0' on unfilled orders and we don't want to clobber NULL with 0."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f != 0.0 else None


def _parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        # Alpaca returns ISO 8601 with 'Z' suffix
        s = str(v).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None
