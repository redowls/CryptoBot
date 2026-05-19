"""Backfill OHLCV history from Alpaca into market_data.

CLI:
    python -m data.ingest --symbol BTC/USD --timeframe 4Hour --days 365

alpaca-py's CryptoHistoricalDataClient auto-paginates a single request via
next_page_token, so a single get_bars_range call will return the full window.
We still chunk the request by `chunk_days` so memory stays bounded on multi-year
backfills and per-chunk progress is visible. The (symbol, timeframe, ts) unique
constraint plus MERGE in Repository.insert_bars makes re-runs idempotent.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv

from data.alpaca_data import AlpacaDataClient
from db.connection import Database
from db.repository import Repository

DEFAULT_CHUNK_DAYS = 90

_UNIT_MAP = {
    "min": TimeFrameUnit.Minute,
    "minute": TimeFrameUnit.Minute,
    "hour": TimeFrameUnit.Hour,
    "day": TimeFrameUnit.Day,
    "week": TimeFrameUnit.Week,
    "month": TimeFrameUnit.Month,
}


def parse_timeframe(s: str) -> TimeFrame:
    """Parse '4Hour', '15Min', '1Day' into alpaca TimeFrame."""
    m = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]+)\s*", s)
    if not m:
        raise ValueError(f"Invalid timeframe: {s!r}")
    amount = int(m.group(1))
    unit_key = m.group(2).lower().rstrip("s")
    if unit_key not in _UNIT_MAP:
        raise ValueError(f"Unknown timeframe unit: {m.group(2)!r}")
    return TimeFrame(amount=amount, unit=_UNIT_MAP[unit_key])


def _to_naive_utc(t) -> datetime:
    ts = pd.Timestamp(t)
    if ts.tz is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.to_pydatetime()


def _existing_ts(
    db: Database,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> set[datetime]:
    sql = """
    SELECT ts FROM market_data
     WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ?;
    """
    df = db.query_df(sql, (symbol, timeframe, _to_naive_utc(start), _to_naive_utc(end)))
    if df.empty:
        return set()
    return {_to_naive_utc(t) for t in df["ts"]}


def backfill_history(
    symbol: str,
    timeframe: str,
    days_back: int,
    *,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    data_client: AlpacaDataClient | None = None,
    repo: Repository | None = None,
    verbose: bool = True,
) -> dict[str, int]:
    """Fetch `days_back` of bars and upsert into market_data via MERGE.

    Returns {'fetched', 'inserted', 'skipped'} where 'skipped' is the count of
    fetched bars whose ts already existed (MERGE updated them in place).
    """
    if days_back <= 0:
        raise ValueError("days_back must be > 0")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be > 0")

    tf = parse_timeframe(timeframe)
    client = data_client or AlpacaDataClient()
    repository = repo or Repository()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)

    existing = _existing_ts(repository.db, symbol, timeframe, start, end)

    fetched_total = 0
    inserted_total = 0
    skipped_total = 0

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        df = client.get_bars_range(symbol, tf, chunk_start, chunk_end)

        if not df.empty:
            df = df.copy()
            df["symbol"] = symbol
            df["timeframe"] = timeframe

            ts_naive = [_to_naive_utc(t) for t in df["ts"]]
            new_count = sum(1 for t in ts_naive if t not in existing)
            dup_count = len(ts_naive) - new_count

            repository.insert_bars(df)
            existing.update(ts_naive)

            fetched_total += len(df)
            inserted_total += new_count
            skipped_total += dup_count

        if verbose:
            print(
                f"  [{chunk_start:%Y-%m-%d} -> {chunk_end:%Y-%m-%d}] "
                f"bars={len(df)}"
            )
        chunk_start = chunk_end

    return {
        "fetched": fetched_total,
        "inserted": inserted_total,
        "skipped": skipped_total,
    }


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Backfill Alpaca crypto OHLCV bars into SQL Server market_data.",
    )
    parser.add_argument("--symbol", required=True, help="e.g. BTC/USD")
    parser.add_argument("--timeframe", required=True, help="e.g. 4Hour, 1Day, 15Min")
    parser.add_argument("--days", type=int, required=True, help="lookback days")
    parser.add_argument(
        "--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS,
        help=f"per-request window in days (default {DEFAULT_CHUNK_DAYS})",
    )
    args = parser.parse_args()

    print(f"Backfilling {args.symbol} {args.timeframe} for {args.days}d...")
    summary = backfill_history(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days_back=args.days,
        chunk_days=args.chunk_days,
    )
    print(
        f"Done. fetched={summary['fetched']} "
        f"inserted={summary['inserted']} "
        f"skipped={summary['skipped']}"
    )


if __name__ == "__main__":
    main()
