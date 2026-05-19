"""Repository: parameterized writes to the cryptobot tables."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

import pandas as pd

from db.connection import Database


class Repository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # ---------- market_data ----------
    def insert_bars(self, df: pd.DataFrame) -> int:
        """Upsert OHLCV bars keyed on (symbol, timeframe, ts).

        Expected columns: symbol, timeframe, ts, open, high, low, close,
        volume, trade_count, vwap. Uses MERGE so re-ingestion is idempotent.
        """
        if df.empty:
            return 0

        required = {
            "symbol", "timeframe", "ts",
            "open", "high", "low", "close", "volume",
        }
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"insert_bars: missing columns {sorted(missing)}")

        rows = [
            (
                str(r.symbol),
                str(r.timeframe),
                _to_utc_naive(r.ts),
                _dec(r.open, scale=8),
                _dec(r.high, scale=8),
                _dec(r.low, scale=8),
                _dec(r.close, scale=8),
                _dec(r.volume, scale=8),
                _int_or_none(getattr(r, "trade_count", None)),
                _dec_or_none(getattr(r, "vwap", None), scale=8),
            )
            for r in df.itertuples(index=False)
        ]

        sql = """
        MERGE market_data AS tgt
        USING (SELECT
                    ? AS symbol, ? AS timeframe, ? AS ts,
                    ? AS open_px, ? AS high_px, ? AS low_px, ? AS close_px,
                    ? AS volume, ? AS trade_count, ? AS vwap
              ) AS src
            ON tgt.symbol = src.symbol
           AND tgt.timeframe = src.timeframe
           AND tgt.ts = src.ts
        WHEN MATCHED THEN UPDATE SET
            open_px = src.open_px,
            high_px = src.high_px,
            low_px = src.low_px,
            close_px = src.close_px,
            volume = src.volume,
            trade_count = src.trade_count,
            vwap = src.vwap
        WHEN NOT MATCHED THEN
            INSERT (symbol, timeframe, ts, open_px, high_px, low_px, close_px,
                    volume, trade_count, vwap)
            VALUES (src.symbol, src.timeframe, src.ts, src.open_px, src.high_px,
                    src.low_px, src.close_px, src.volume, src.trade_count, src.vwap);
        """
        return self.db.executemany(sql, rows)

    # ---------- signals ----------
    def insert_signal(
        self,
        symbol: str,
        strategy: str,
        side: str,
        signal_price: float | Decimal,
        atr: float | Decimal | None = None,
        proposed_qty: float | Decimal | None = None,
        proposed_stop: float | Decimal | None = None,
        notes: str | None = None,
        ts: datetime | None = None,
    ) -> int:
        sql = """
        INSERT INTO signals
            (ts, symbol, strategy, side, signal_price, atr,
             proposed_qty, proposed_stop, notes)
        OUTPUT INSERTED.id
        VALUES (COALESCE(?, SYSUTCDATETIME()), ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            _to_utc_naive(ts) if ts else None,
            symbol, strategy, side,
            _dec(signal_price),
            _dec_or_none(atr),
            _dec_or_none(proposed_qty),
            _dec_or_none(proposed_stop),
            notes,
        )
        return self._insert_returning_id(sql, params)

    # ---------- orders ----------
    def insert_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        type: str,
        qty: float | Decimal,
        status: str,
        signal_id: int | None = None,
        alpaca_order_id: str | None = None,
        limit_price: float | Decimal | None = None,
        raw_response: str | None = None,
    ) -> int:
        sql = """
        INSERT INTO orders
            (client_order_id, alpaca_order_id, signal_id, symbol, side, type,
             qty, limit_price, status, raw_response)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            client_order_id, alpaca_order_id, signal_id, symbol, side, type,
            _dec(qty), _dec_or_none(limit_price), status, raw_response,
        )
        return self._insert_returning_id(sql, params)

    def update_order_status(
        self,
        client_order_id: str,
        status: str,
        alpaca_order_id: str | None = None,
        filled_qty: float | Decimal | None = None,
        filled_avg_price: float | Decimal | None = None,
        filled_at: datetime | None = None,
        raw_response: str | None = None,
    ) -> int:
        sql = """
        UPDATE orders
           SET status = ?,
               alpaca_order_id  = COALESCE(?, alpaca_order_id),
               filled_qty       = COALESCE(?, filled_qty),
               filled_avg_price = COALESCE(?, filled_avg_price),
               filled_at        = COALESCE(?, filled_at),
               raw_response     = COALESCE(?, raw_response)
         WHERE client_order_id = ?;
        """
        params = (
            status,
            alpaca_order_id,
            _dec_or_none(filled_qty),
            _dec_or_none(filled_avg_price),
            _to_utc_naive(filled_at) if filled_at else None,
            raw_response,
            client_order_id,
        )
        return self.db.execute(sql, params)

    # ---------- positions ----------
    def upsert_position(
        self,
        symbol: str,
        qty: float | Decimal,
        avg_entry_price: float | Decimal,
        current_stop: float | Decimal | None = None,
        opened_at: datetime | None = None,
    ) -> int:
        sql = """
        MERGE positions AS tgt
        USING (SELECT ? AS symbol, ? AS qty, ? AS avg_entry_price,
                      ? AS current_stop, ? AS opened_at) AS src
            ON tgt.symbol = src.symbol
        WHEN MATCHED THEN UPDATE SET
            qty = src.qty,
            avg_entry_price = src.avg_entry_price,
            current_stop = COALESCE(src.current_stop, tgt.current_stop),
            last_updated = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (symbol, qty, avg_entry_price, current_stop, opened_at)
            VALUES (src.symbol, src.qty, src.avg_entry_price, src.current_stop,
                    COALESCE(src.opened_at, SYSUTCDATETIME()));
        """
        params = (
            symbol,
            _dec(qty),
            _dec(avg_entry_price),
            _dec_or_none(current_stop),
            _to_utc_naive(opened_at) if opened_at else None,
        )
        return self.db.execute(sql, params)

    def delete_position(self, symbol: str) -> int:
        return self.db.execute("DELETE FROM positions WHERE symbol = ?;", (symbol,))

    def get_open_positions_with_stops(self) -> list[dict]:
        """All rows from `positions` as plain dicts. current_stop is float | None.

        Used by risk.manager.monitor_stops — every 1-minute tick reads this and
        diffs each stop against the latest Alpaca bid.
        """
        df = self.db.query_df(
            "SELECT symbol, qty, avg_entry_price, current_stop, opened_at FROM positions;"
        )
        if df.empty:
            return []
        out: list[dict] = []
        for _, r in df.iterrows():
            cs = r["current_stop"]
            out.append({
                "symbol": str(r["symbol"]),
                "qty": float(r["qty"]),
                "avg_entry_price": float(r["avg_entry_price"]),
                "current_stop": float(cs) if cs is not None and not pd.isna(cs) else None,
                "opened_at": r["opened_at"],
            })
        return out

    def count_open_positions(self) -> int:
        df = self.db.query_df("SELECT COUNT(*) AS n FROM positions;")
        return int(df.iloc[0]["n"]) if not df.empty else 0

    # ---------- trades ----------
    def record_closed_trade(
        self,
        *,
        symbol: str,
        qty: float | Decimal,
        entry_price: float | Decimal,
        exit_price: float | Decimal,
        entry_ts: datetime,
        exit_ts: datetime,
        entry_order_id: int | None = None,
        exit_order_id: int | None = None,
        fees_usd: float | Decimal | None = None,
    ) -> int:
        """INSERT a CLOSED trade row with computed pnl. Long-only:
            pnl_usd = (exit - entry) * qty - fees
            pnl_pct = pnl_usd / (entry * qty)
        """
        qty_f = float(qty)
        ep = float(entry_price)
        xp = float(exit_price)
        fees = float(fees_usd) if fees_usd is not None else 0.0
        notional = ep * qty_f
        pnl = (xp - ep) * qty_f - fees
        pct = (pnl / notional) if notional > 0 else 0.0

        sql = """
        INSERT INTO trades
            (symbol, entry_order_id, exit_order_id, entry_ts, exit_ts,
             entry_price, exit_price, qty, pnl_usd, pnl_pct, fees_usd, status)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED');
        """
        params = (
            symbol, entry_order_id, exit_order_id,
            _to_utc_naive(entry_ts), _to_utc_naive(exit_ts),
            _dec(entry_price, scale=8), _dec(exit_price, scale=8),
            _dec(qty, scale=8),
            _dec(pnl, scale=8), _dec(pct, scale=4),
            _dec_or_none(fees_usd, scale=8),
        )
        return self._insert_returning_id(sql, params)

    # ---------- account_snapshots ----------
    def insert_snapshot(
        self,
        equity: float | Decimal,
        cash: float | Decimal,
        buying_power: float | Decimal,
        portfolio_value: float | Decimal,
        ts: datetime | None = None,
    ) -> int:
        sql = """
        INSERT INTO account_snapshots
            (ts, equity, cash, buying_power, portfolio_value)
        OUTPUT INSERTED.id
        VALUES (COALESCE(?, SYSUTCDATETIME()), ?, ?, ?, ?);
        """
        params = (
            _to_utc_naive(ts) if ts else None,
            _dec(equity), _dec(cash), _dec(buying_power), _dec(portfolio_value),
        )
        return self._insert_returning_id(sql, params)

    def get_peak_equity(self, since: datetime | None = None) -> float | None:
        """Highest equity ever recorded (optionally from `since` onward). Used
        by RiskManager.check_drawdown for the peak-to-trough kill switch."""
        if since is not None:
            df = self.db.query_df(
                "SELECT MAX(equity) AS peak FROM account_snapshots WHERE ts >= ?;",
                (_to_utc_naive(since),),
            )
        else:
            df = self.db.query_df("SELECT MAX(equity) AS peak FROM account_snapshots;")
        if df.empty:
            return None
        v = df.iloc[0]["peak"]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)

    def get_daily_anchor_equity(self, now: datetime | None = None) -> float | None:
        """Equity benchmark for "today's P&L %" — the first snapshot on or after
        today's 00:00 UTC; if none yet, falls back to the most recent prior
        snapshot. Used by RiskManager.check_daily_loss.
        """
        now = now or datetime.now(timezone.utc)
        day_start = now.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_start_naive = _to_utc_naive(day_start)

        df = self.db.query_df(
            "SELECT TOP 1 equity FROM account_snapshots "
            "WHERE ts >= ? ORDER BY ts ASC;",
            (day_start_naive,),
        )
        if not df.empty:
            return float(df.iloc[0]["equity"])

        df = self.db.query_df(
            "SELECT TOP 1 equity FROM account_snapshots "
            "WHERE ts < ? ORDER BY ts DESC;",
            (day_start_naive,),
        )
        if not df.empty:
            return float(df.iloc[0]["equity"])
        return None

    # ---------- logs ----------
    def log(
        self,
        level: str,
        module: str | None,
        msg: str,
        exc: BaseException | str | None = None,
    ) -> int:
        if isinstance(exc, BaseException):
            exc_text: str | None = f"{type(exc).__name__}: {exc}"
        else:
            exc_text = exc
        sql = """
        INSERT INTO logs (level, module, message, exception)
        VALUES (?, ?, ?, ?);
        """
        return self.db.execute(sql, (level, module, msg, exc_text))

    # ---------- internals ----------
    def _insert_returning_id(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(row[0])


# ---------- value coercion helpers ----------

def _dec(v: Any, scale: int | None = None) -> Decimal:
    """Coerce to Decimal; if `scale` is set, quantize to that many fractional
    digits with banker's rounding. Use scale when writing to a DECIMAL(*, scale)
    column — float→str can produce trailing-precision noise (e.g.
    0.5764056250000001) that would otherwise overflow the column's scale.
    """
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    if scale is None:
        return d
    q = Decimal(1).scaleb(-scale)
    return d.quantize(q, rounding=ROUND_HALF_EVEN)


def _dec_or_none(v: Any, scale: int | None = None) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    return _dec(v, scale=scale)


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    return int(v)


def _to_utc_naive(ts: Any) -> datetime:
    """Convert any timestamp into a naive UTC datetime for DATETIME2 columns."""
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if not isinstance(ts, datetime):
        ts = pd.Timestamp(ts).to_pydatetime()
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts
