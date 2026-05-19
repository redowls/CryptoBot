"""Smoke test: fetch 30 days of BTC/USD 4h bars and print summary stats.

Runnable two ways:
    python -m tests.test_data            # script mode, prints stats
    python -m pytest tests/test_data.py  # pytest mode, asserts only
"""

from __future__ import annotations

from dotenv import load_dotenv
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from data.alpaca_data import AlpacaDataClient

load_dotenv()

FOUR_HOUR = TimeFrame(amount=4, unit=TimeFrameUnit.Hour)


def fetch_btc_30d():
    client = AlpacaDataClient()
    df = client.get_bars("BTC/USD", FOUR_HOUR, lookback_days=30)
    return df, client


def test_btc_30d_bars():
    df, _ = fetch_btc_30d()
    # 30 days * 6 bars/day = 180 nominal; allow slack for partial current bar / gaps.
    assert len(df) >= 150, f"expected ~180 bars, got {len(df)}"
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume", "trade_count", "vwap"]
    assert df["ts"].is_monotonic_increasing
    assert df["ts"].dt.tz is not None, "ts must be tz-aware UTC"
    assert (df["close"] > 0).all()
    assert (df["high"] >= df["low"]).all()


if __name__ == "__main__":
    df, client = fetch_btc_30d()
    print(f"Fetched {len(df)} bars for BTC/USD 4Hour")
    print(f"Range: {df['ts'].min()} -> {df['ts'].max()}")
    print(f"Close range: {df['close'].min():.2f} -> {df['close'].max():.2f}")
    print(f"Mean close: {df['close'].mean():.2f}")
    print(f"Mean volume: {df['volume'].mean():.4f}")
    print("\nFirst 3 bars:")
    print(df.head(3).to_string(index=False))
    print("\nLast 3 bars:")
    print(df.tail(3).to_string(index=False))

    try:
        quote = client.get_latest_quote("BTC/USD")
        print(f"\nLatest BTC/USD quote: bid={quote.bid_price} ask={quote.ask_price}")
    except Exception as e:
        print(f"\nLatest quote skipped: {e}")

    try:
        acct = client.get_account_info()
        print(f"Account status: {acct.status} | equity={acct.equity} | cash={acct.cash}")
    except Exception as e:
        print(f"Account info skipped (no API keys?): {e}")
