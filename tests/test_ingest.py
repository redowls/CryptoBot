"""Smoke test for data.ingest.backfill_history.

Hits real Alpaca (free historical crypto data, no keys needed) but uses an
in-memory fake repository so no SQL Server is required. Verifies:

  - timeframe parser handles common forms
  - first backfill counts all bars as inserted
  - second backfill against the same fake DB counts all bars as skipped
    (idempotency contract of the MERGE upsert)

Runnable two ways:
    python -m tests.test_ingest             # script mode, prints summaries
    python -m pytest tests/test_ingest.py   # pytest mode
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from dotenv import load_dotenv

from data.alpaca_data import AlpacaDataClient
from data.ingest import backfill_history, parse_timeframe

load_dotenv()

SMOKE_SYMBOL = "BTC/USD"
SMOKE_TIMEFRAME = "4Hour"
SMOKE_DAYS = 5


class _FakeDatabase:
    """Stand-in for db.connection.Database used by Repository._existing_ts query."""

    def __init__(self, store: set[tuple[str, str, datetime]]) -> None:
        self.store = store

    def query_df(self, sql: str, params) -> pd.DataFrame:
        symbol, timeframe, start, end = params
        rows = [
            t for (s, tf, t) in self.store
            if s == symbol and tf == timeframe and start <= t <= end
        ]
        return pd.DataFrame({"ts": rows}) if rows else pd.DataFrame()


class _FakeRepository:
    """Stand-in for db.repository.Repository — captures what insert_bars would write."""

    def __init__(self) -> None:
        self.store: set[tuple[str, str, datetime]] = set()
        self.db = _FakeDatabase(self.store)
        self.insert_calls = 0

    def insert_bars(self, df: pd.DataFrame) -> int:
        self.insert_calls += 1
        for r in df.itertuples(index=False):
            ts = pd.Timestamp(r.ts)
            if ts.tz is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            self.store.add((str(r.symbol), str(r.timeframe), ts.to_pydatetime()))
        return len(df)


def test_parse_timeframe():
    assert parse_timeframe("4Hour").value == "4Hour"
    assert parse_timeframe("15Min").value == "15Min"
    assert parse_timeframe("1Day").value == "1Day"
    # tolerant of plural and case
    assert parse_timeframe("2Hours").value == "2Hour"
    assert parse_timeframe("1day").value == "1Day"
    with pytest.raises(ValueError):
        parse_timeframe("nonsense")
    with pytest.raises(ValueError):
        parse_timeframe("5Fortnight")


def test_backfill_history_idempotent():
    repo = _FakeRepository()
    client = AlpacaDataClient()

    first = backfill_history(
        symbol=SMOKE_SYMBOL,
        timeframe=SMOKE_TIMEFRAME,
        days_back=SMOKE_DAYS,
        data_client=client,
        repo=repo,
        verbose=False,
    )
    assert first["fetched"] > 0, "no bars fetched from Alpaca — check network/SDK"
    assert first["inserted"] == first["fetched"]
    assert first["skipped"] == 0

    # Re-running against the same fake store must classify every bar as a duplicate.
    second = backfill_history(
        symbol=SMOKE_SYMBOL,
        timeframe=SMOKE_TIMEFRAME,
        days_back=SMOKE_DAYS,
        data_client=client,
        repo=repo,
        verbose=False,
    )
    assert second["fetched"] > 0
    assert second["inserted"] == 0
    assert second["skipped"] == second["fetched"]


if __name__ == "__main__":
    repo = _FakeRepository()
    client = AlpacaDataClient()

    print(f"First pass: backfilling {SMOKE_SYMBOL} {SMOKE_TIMEFRAME} for {SMOKE_DAYS}d...")
    s1 = backfill_history(SMOKE_SYMBOL, SMOKE_TIMEFRAME, SMOKE_DAYS,
                          data_client=client, repo=repo)
    print(f"  -> fetched={s1['fetched']} inserted={s1['inserted']} skipped={s1['skipped']}")

    print("\nSecond pass (idempotency check):")
    s2 = backfill_history(SMOKE_SYMBOL, SMOKE_TIMEFRAME, SMOKE_DAYS,
                          data_client=client, repo=repo)
    print(f"  -> fetched={s2['fetched']} inserted={s2['inserted']} skipped={s2['skipped']}")

    print(f"\nFake repo holds {len(repo.store)} unique (symbol, timeframe, ts) rows "
          f"after {repo.insert_calls} insert_bars calls.")
