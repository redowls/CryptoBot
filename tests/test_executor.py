"""Executor tests + script-mode synthetic signal.

Pytest mode covers the unit-testable pieces: client_order_id format, position
normalization, and reconcile diffing (against a fake TradingClient that doesn't
hit the network).

Script mode (per Session 7 acceptance test) places a *real* market order on
the paper account and verifies it shows up in `orders`. Defaults to a $10
notional BTC/USD BUY — well above Alpaca's ~$1 crypto minimum, small enough
that paper PnL noise is irrelevant. Skip script mode if Alpaca keys aren't set.

Run modes:
    python -m pytest tests/test_executor.py            # offline unit tests
    python -m tests.test_executor                      # submits a paper order
    python -m tests.test_executor --symbol ETH/USD     # different pair
    python -m tests.test_executor --notional 5         # smaller order
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from dotenv import load_dotenv

from execution.alpaca_executor import (
    Executor,
    _normalize_crypto_symbol,
    _nonzero,
)

load_dotenv()


# ---------- pure helpers ----------

def test_client_order_id_format():
    coid = Executor.make_client_order_id("BTC/USD")
    # Shape: BTCUSD-YYYYMMDDHHMMSS-XXXXXX (3 dashes total, parts of right lengths)
    parts = coid.split("-")
    assert len(parts) == 3, f"expected 3 dash-parts, got {coid!r}"
    sym, ts, suffix = parts
    assert sym == "BTCUSD"
    assert len(ts) == 14 and ts.isdigit()
    assert len(suffix) == 6


def test_client_order_id_unique_across_calls():
    """uuid6 suffix means two back-to-back calls in the same second don't collide."""
    a = Executor.make_client_order_id("BTC/USD")
    b = Executor.make_client_order_id("BTC/USD")
    assert a != b


def test_normalize_crypto_symbol_slash_passthrough():
    assert _normalize_crypto_symbol("BTC/USD") == "BTC/USD"


def test_normalize_crypto_symbol_inserts_slash():
    assert _normalize_crypto_symbol("BTCUSD") == "BTC/USD"
    assert _normalize_crypto_symbol("ETHUSDT") == "ETH/USDT"
    assert _normalize_crypto_symbol("SOLBTC") == "SOL/BTC"


def test_nonzero_treats_zero_as_none():
    # Alpaca reports filled_qty='0' on unfilled orders; we want to leave the
    # corresponding column NULL rather than clobbering it.
    assert _nonzero("0") is None
    assert _nonzero(0.0) is None
    assert _nonzero(None) is None
    assert _nonzero("1.5") == 1.5
    assert _nonzero("abc") is None


# ---------- reconcile_positions with a fake trading client ----------

@dataclass
class _FakePos:
    symbol: str
    qty: str = "0.1"
    avg_entry_price: str = "30000.00"


class _FakeRepo:
    """In-memory stand-in for Repository — captures upserts/deletes/queries."""
    def __init__(self, existing_symbols: list[str] | None = None) -> None:
        self.existing = list(existing_symbols or [])
        self.upserts: list[dict] = []
        self.deletes: list[str] = []
        self.db = MagicMock()
        self.db.query_df.return_value = pd.DataFrame({"symbol": self.existing})

    def upsert_position(self, **kwargs: Any) -> int:
        self.upserts.append(kwargs)
        return 1

    def delete_position(self, symbol: str) -> int:
        self.deletes.append(symbol)
        return 1


def _executor_with_fake(repo: _FakeRepo, alpaca_positions: list[_FakePos]) -> Executor:
    fake_trading = MagicMock()
    fake_trading.get_all_positions.return_value = alpaca_positions
    return Executor(repo=repo, trading_client=fake_trading)  # type: ignore[arg-type]


def test_reconcile_adds_alpaca_only_position():
    repo = _FakeRepo(existing_symbols=[])  # SQL has nothing
    ex = _executor_with_fake(repo, [_FakePos(symbol="BTC/USD")])
    out = ex.reconcile_positions()
    assert out == {"added": 1, "updated": 0, "removed": 0}
    assert len(repo.upserts) == 1
    assert repo.upserts[0]["symbol"] == "BTC/USD"
    assert repo.upserts[0]["qty"] == pytest.approx(0.1)
    assert repo.upserts[0]["current_stop"] is None  # COALESCE preserves
    assert repo.deletes == []


def test_reconcile_deletes_sql_only_position():
    repo = _FakeRepo(existing_symbols=["ETH/USD"])  # SQL has ETH/USD, Alpaca doesn't
    ex = _executor_with_fake(repo, [])
    out = ex.reconcile_positions()
    assert out == {"added": 0, "updated": 0, "removed": 1}
    assert repo.deletes == ["ETH/USD"]
    assert repo.upserts == []


def test_reconcile_updates_when_both_have_position():
    repo = _FakeRepo(existing_symbols=["BTC/USD"])
    ex = _executor_with_fake(repo, [_FakePos(symbol="BTC/USD", qty="0.2",
                                              avg_entry_price="31000")])
    out = ex.reconcile_positions()
    assert out == {"added": 0, "updated": 1, "removed": 0}
    assert repo.upserts[0]["qty"] == pytest.approx(0.2)
    assert repo.upserts[0]["avg_entry_price"] == pytest.approx(31000)
    # current_stop=None lets the MERGE's COALESCE preserve whatever stop the
    # risk module set last cycle.
    assert repo.upserts[0]["current_stop"] is None


def test_reconcile_normalizes_unslashed_alpaca_symbol():
    """If Alpaca returns 'BTCUSD' we still match against SQL key 'BTC/USD'."""
    repo = _FakeRepo(existing_symbols=["BTC/USD"])
    ex = _executor_with_fake(repo, [_FakePos(symbol="BTCUSD")])
    out = ex.reconcile_positions()
    assert out["updated"] == 1
    assert out["added"] == 0
    assert repo.upserts[0]["symbol"] == "BTC/USD"


def test_reconcile_handles_negative_qty():
    """Crypto doesn't short, but defensive: abs() so reconcile never writes negatives."""
    repo = _FakeRepo(existing_symbols=[])
    ex = _executor_with_fake(repo, [_FakePos(symbol="BTC/USD", qty="-0.1")])
    ex.reconcile_positions()
    assert repo.upserts[0]["qty"] == pytest.approx(0.1)


# ---------- close_position ----------
# Bug context: Alpaca holds positions to 9+ decimal places, but our SQL
# `positions.qty` is DECIMAL(28,8). Reconcile rounds Alpaca's value, and a
# later SELL using the rounded SQL qty is rejected with HTTP 403 +
# code:40310000 "insufficient balance" because the request exceeds the true
# holding by a few satoshis. `close_position` delegates the qty decision to
# Alpaca's own balance via TradingClient.close_position(symbol), bypassing
# the round-trip through our DECIMAL column.


class _OrderRepo:
    """Capture insert_order / update_order_status calls for close_position tests."""
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.updated: list[dict] = []

    def insert_order(self, **kwargs: Any) -> int:
        self.inserted.append(kwargs)
        return len(self.inserted)

    def update_order_status(self, **kwargs: Any) -> int:
        self.updated.append(kwargs)
        return 1


def _close_position_response(**overrides: Any) -> MagicMock:
    """Build a fake alpaca-py Order returned from close_position()."""
    raw = {
        "id": "alpaca-order-xyz",
        "client_order_id": "BTCUSD-20260523120000-abcdef",
        "status": "accepted",
        "filled_qty": "0",
        "filled_avg_price": None,
        "filled_at": None,
    }
    raw.update(overrides)
    m = MagicMock()
    m.model_dump.return_value = raw
    return m


def test_close_position_calls_trading_close_not_submit_order():
    """The whole point of close_position: it must NOT pass a qty to Alpaca.

    Using TradingClient.submit_order with qty=db_qty is what triggers the
    dust-rounding 403. close_position(symbol) uses Alpaca's authoritative
    balance internally — no qty arg.
    """
    repo = _OrderRepo()
    fake_trading = MagicMock()
    fake_trading.close_position.return_value = _close_position_response()
    ex = Executor(repo=repo, trading_client=fake_trading)  # type: ignore[arg-type]

    ex.close_position(symbol="BTC/USD", qty_hint=0.00012945, signal_id=42)

    fake_trading.close_position.assert_called_once_with("BTC/USD")
    fake_trading.submit_order.assert_not_called()


def test_close_position_pre_inserts_orders_row_with_status_pending():
    """Audit-trail invariant: orders row exists with status='pending' BEFORE the
    API call, so a crash mid-flight still leaves something to recover from."""
    repo = _OrderRepo()
    fake_trading = MagicMock()
    fake_trading.close_position.return_value = _close_position_response()
    ex = Executor(repo=repo, trading_client=fake_trading)  # type: ignore[arg-type]

    ex.close_position(symbol="BTC/USD", qty_hint=0.00012945, signal_id=42)

    assert len(repo.inserted) == 1
    row = repo.inserted[0]
    assert row["symbol"] == "BTC/USD"
    assert row["side"] == "SELL"
    assert row["type"] == "market"
    assert row["status"] == "pending"
    assert row["signal_id"] == 42
    # qty_hint flows into the orders row as audit context — we needed *some*
    # number for the NOT NULL column. Real fill qty lands in filled_qty via
    # update_order_status after the API responds.
    assert float(row["qty"]) == pytest.approx(0.00012945)


def test_close_position_updates_orders_row_with_alpaca_response():
    repo = _OrderRepo()
    fake_trading = MagicMock()
    fake_trading.close_position.return_value = _close_position_response(
        id="alpaca-order-zzz",
        status="filled",
        filled_qty="0.000129445",
        filled_avg_price="75800.50",
        filled_at="2026-05-23T12:00:01Z",
    )
    ex = Executor(repo=repo, trading_client=fake_trading)  # type: ignore[arg-type]

    ex.close_position(symbol="BTC/USD", qty_hint=0.00012945)

    assert len(repo.updated) == 1
    upd = repo.updated[0]
    assert upd["status"] == "filled"
    assert upd["alpaca_order_id"] == "alpaca-order-zzz"
    assert float(upd["filled_qty"]) == pytest.approx(0.000129445)
    assert float(upd["filled_avg_price"]) == pytest.approx(75800.50)


def test_close_position_returns_submitted_order():
    repo = _OrderRepo()
    fake_trading = MagicMock()
    fake_trading.close_position.return_value = _close_position_response(
        status="filled", filled_qty="0.000129445",
    )
    ex = Executor(repo=repo, trading_client=fake_trading)  # type: ignore[arg-type]

    submitted = ex.close_position(symbol="BTC/USD", qty_hint=0.00012945)

    assert submitted.symbol == "BTC/USD"
    assert submitted.side == "SELL"
    assert submitted.status == "filled"
    assert submitted.alpaca_order_id == "alpaca-order-xyz"
    assert submitted.client_order_id  # auto-generated


def test_close_position_marks_orders_row_error_when_alpaca_raises():
    """If close_position raises, the pre-inserted row must end up as 'error'
    (not stuck in 'pending') so recovery sweeps can tell what happened."""
    repo = _OrderRepo()
    fake_trading = MagicMock()
    fake_trading.close_position.side_effect = RuntimeError("403 forbidden")
    ex = Executor(repo=repo, trading_client=fake_trading)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        ex.close_position(symbol="BTC/USD", qty_hint=0.00012945)

    assert len(repo.inserted) == 1  # pre-insert still happened
    assert len(repo.updated) == 1
    assert repo.updated[0]["status"] == "error"


# ---------- script mode: submit a real paper order ----------

def _script_main() -> None:
    """Submit a small notional BUY on the configured paper account."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USD",
                        help="pair to buy (default BTC/USD)")
    parser.add_argument("--notional", type=float, default=10.0,
                        help="USD notional (default 10)")
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    args = parser.parse_args()

    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY must be set in .env")

    from data.alpaca_data import AlpacaDataClient
    from db.repository import Repository

    repo = Repository()
    data_client = AlpacaDataClient(paper=True)
    quote = data_client.get_latest_quote(args.symbol)
    ask = float(quote.ask_price)
    qty = round(args.notional / ask, 8)
    print(f"Latest ask {args.symbol}: {ask:.2f}  ->  qty={qty} for ${args.notional} notional")

    ex = Executor(repo=repo, paper=True)
    submitted = ex.submit_market_order(
        symbol=args.symbol, qty=qty, side=args.side,
    )
    print(f"\nSubmitted:")
    print(f"  client_order_id : {submitted.client_order_id}")
    print(f"  alpaca_order_id : {submitted.alpaca_order_id}")
    print(f"  status          : {submitted.status}")
    print(f"  qty             : {submitted.qty}")
    print(f"  side            : {submitted.side}")

    # Confirm the row landed in `orders`.
    df = repo.db.query_df(
        "SELECT client_order_id, alpaca_order_id, symbol, side, qty, status, "
        "submitted_at FROM orders WHERE client_order_id = ?;",
        (submitted.client_order_id,),
    )
    print("\norders row:")
    print(df.to_string(index=False))

    # And a brief positions snapshot via reconcile (Alpaca may not have filled
    # yet for market orders on first poll, but the row will appear shortly).
    recon = ex.reconcile_positions()
    print(f"\nreconcile after submit: {recon}")


if __name__ == "__main__":
    _script_main()
