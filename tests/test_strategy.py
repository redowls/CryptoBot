"""Strategy unit tests + script-mode signal replay.

pytest assertions use synthetic OHLC series — fully deterministic, no network,
no DB. The script mode (`python -m tests.test_strategy`) loads real bars from
SQL Server if available, else falls back to a live Alpaca fetch, then prints
every historical signal that the strategy would have emitted.

    python -m pytest tests/test_strategy.py    # pytest mode
    python -m tests.test_strategy              # script: prints signals
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from dotenv import load_dotenv

from strategy.donchian import (
    DonchianStrategy,
    Signal,
    _wilder_atr,
    replay_signals,
)

load_dotenv()


# ---------- synthetic fixtures ----------

def _flat_4h(n: int, price: float, ts_start: str = "2025-01-01") -> pd.DataFrame:
    ts = pd.date_range(ts_start, periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({
        "ts": ts,
        "open": price, "high": price, "low": price, "close": price,
        "volume": 1.0,
    })


def _uptrend_daily(n: int = 250) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = pd.Series(range(n), dtype=float) + 100.0
    return pd.DataFrame({
        "ts": ts,
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "volume": 1.0,
    })


def _downtrend_daily(n: int = 250) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = 1000.0 - pd.Series(range(n), dtype=float)
    return pd.DataFrame({
        "ts": ts,
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "volume": 1.0,
    })


# ---------- indicators ----------

def test_donchian_high_shifted_by_one():
    """donchian_high at row t must be max(high[t-N..t-1]), excluding bar t itself."""
    highs = [float(i) for i in range(1, 31)]
    df = pd.DataFrame({"high": highs, "low": highs, "close": highs})
    strat = DonchianStrategy(donchian_entry=20, donchian_exit=10, atr_period=14)
    ind = strat.compute_indicators(df)

    # Row 19 is the 20th bar — we have only 19 prior bars, so still NaN.
    assert pd.isna(ind["donchian_high"].iloc[19])
    # Row 20: max of highs[0..19] = 20.0 (the highest of the first 20 highs).
    assert ind["donchian_high"].iloc[20] == 20.0
    # Row 21: max of highs[1..20] = 21.0. Shows the window slid forward.
    assert ind["donchian_high"].iloc[21] == 21.0
    # Look-ahead canary: must NOT equal the current bar's high.
    assert ind["donchian_high"].iloc[20] != ind["high"].iloc[20]


def test_donchian_low_uses_exit_period():
    lows = [10.0] * 20 + [5.0] + [10.0] * 10
    df = pd.DataFrame({"high": lows, "low": lows, "close": lows})
    strat = DonchianStrategy(donchian_entry=20, donchian_exit=10)
    ind = strat.compute_indicators(df)
    # The 5.0 dip is at index 20. With 10-bar exit channel shifted by 1,
    # rows 21..30 should reflect it; row 31 onwards should not.
    assert ind["donchian_low"].iloc[21] == 5.0
    assert ind["donchian_low"].iloc[30] == 5.0


def test_wilder_atr_matches_ewm_alpha():
    """Wilder smoothing == EWM with alpha = 1/period (not span = period)."""
    df = pd.DataFrame({
        "high":  [10, 11, 12, 13, 14, 15],
        "low":   [ 9, 10, 11, 12, 13, 14],
        "close": [ 9, 11, 11, 13, 13, 15],
    }, dtype=float)
    atr = _wilder_atr(df, period=3)
    # Hand-compute TR: row0 has no prev_close so TR_0 = high-low = 1; row1
    # TR=max(11-10, |11-9|, |10-9|)=2; row2 max(12-11,|12-11|,|11-11|)=1; etc.
    expected_tr = pd.Series([1.0, 2.0, 1.0, 2.0, 1.0, 2.0])
    pd.testing.assert_series_equal(
        expected_tr.ewm(alpha=1 / 3, adjust=False).mean(),
        atr.reset_index(drop=True),
        check_names=False,
    )


# ---------- signal generation ----------

def _breakout_4h() -> pd.DataFrame:
    """50 flat bars at 100, then a single breakout bar to 110."""
    df = _flat_4h(50, 100.0)
    last_ts = df["ts"].iloc[-1] + pd.Timedelta("4h")
    breakout = pd.DataFrame({
        "ts": [last_ts],
        "open": [100.0], "high": [110.0], "low": [100.0], "close": [110.0],
        "volume": [1.0],
    })
    return pd.concat([df, breakout], ignore_index=True)


def test_buy_signal_on_breakout_with_daily_uptrend():
    strat = DonchianStrategy(donchian_entry=20, donchian_exit=10, atr_period=14,
                             atr_stop_mult=2.0)
    sig = strat.generate_signal(_breakout_4h(), _uptrend_daily(), None, "BTC/USD")
    assert isinstance(sig, Signal)
    assert sig.side == "BUY"
    assert sig.symbol == "BTC/USD"
    assert sig.price == 110.0
    # Whatever atr is, stop must equal price - 2*atr.
    assert sig.stop == pytest.approx(sig.price - 2.0 * sig.atr)
    assert "breakout" in sig.reason


def test_no_buy_when_daily_below_200ema():
    strat = DonchianStrategy()
    sig = strat.generate_signal(_breakout_4h(), _downtrend_daily(), None, "BTC/USD")
    assert sig is None


def test_no_signal_when_indicators_not_warmed_up():
    strat = DonchianStrategy(donchian_entry=20)
    short = _flat_4h(10, 100.0)
    sig = strat.generate_signal(short, _uptrend_daily(), None, "BTC/USD")
    assert sig is None


def test_sell_on_close_below_donchian_low():
    """While long, a close below the 10-bar Donchian low triggers SELL."""
    df = _flat_4h(50, 100.0)
    last_ts = df["ts"].iloc[-1] + pd.Timedelta("4h")
    breakdown = pd.DataFrame({
        "ts": [last_ts],
        "open": [100.0], "high": [100.0], "low": [80.0], "close": [80.0],
        "volume": [1.0],
    })
    df = pd.concat([df, breakdown], ignore_index=True)
    strat = DonchianStrategy()
    sig = strat.generate_signal(df, _uptrend_daily(), {"current_stop": 70.0},
                                "BTC/USD")
    assert sig is not None
    assert sig.side == "SELL"
    assert "donchian_low" in sig.reason


def test_sell_on_stop_hit():
    """While long, a close below current_stop (but above donchian_low) triggers SELL."""
    # Bars 0..49: high=100, low=80, close=90 → donchian_low = 80
    n = 50
    ts = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({
        "ts": ts,
        "open": 90.0, "high": 100.0, "low": 80.0, "close": 90.0, "volume": 1.0,
    })
    # Final bar: close=85 → above donchian_low(80), below stop(95).
    last_ts = df["ts"].iloc[-1] + pd.Timedelta("4h")
    final = pd.DataFrame({
        "ts": [last_ts],
        "open": [90.0], "high": [95.0], "low": [85.0], "close": [85.0],
        "volume": [1.0],
    })
    df = pd.concat([df, final], ignore_index=True)

    strat = DonchianStrategy()
    sig = strat.generate_signal(df, _uptrend_daily(),
                                {"current_stop": 95.0}, "BTC/USD")
    assert sig is not None
    assert sig.side == "SELL"
    assert "stop hit" in sig.reason


def test_replay_excludes_in_progress_daily_bar():
    """The daily bar dated the same UTC day as the 4h decision is in-progress
    (its close isn't known yet) and must not be visible to the EMA filter.

    Setup: 250 daily bars in a strong downtrend (so the 200-EMA is well above
    the close), then a single daily bar on the breakout day that prints a huge
    'close' that would flip the EMA filter true. The 4h breakout bar lands on
    that same UTC day. With the naive `daily.ts <= 4h.ts` slice, the spike
    daily bar leaks in and a BUY fires; with the correct slice it does not.
    """
    # 250 daily downtrend bars ending 2025-01-31.
    daily_n = 250
    daily_ts = pd.date_range(end="2025-01-31", periods=daily_n, freq="D", tz="UTC")
    daily_close = pd.Series(range(daily_n, 0, -1), dtype=float)  # 250 -> 1
    df_daily = pd.DataFrame({
        "ts": daily_ts,
        "open": daily_close, "high": daily_close + 1,
        "low": daily_close - 1, "close": daily_close, "volume": 1.0,
    })
    # Append an in-progress daily bar on 2025-02-01 with a fake huge close.
    df_daily = pd.concat([df_daily, pd.DataFrame([{
        "ts": pd.Timestamp("2025-02-01", tz="UTC"),
        "open": 1.0, "high": 9999.0, "low": 1.0, "close": 9999.0, "volume": 1.0,
    }])], ignore_index=True)

    # 30 flat 4h bars then a breakout on 2025-02-01 04:00 UTC (same UTC day as
    # the in-progress daily bar). With the naive slice that daily bar leaks in;
    # with the correct slice the decision still sees only the downtrending tail.
    flat = _flat_4h(30, 100.0, ts_start="2025-01-26")
    breakout_ts = pd.Timestamp("2025-02-01 04:00", tz="UTC")
    breakout = pd.DataFrame({
        "ts": [breakout_ts],
        "open": [100.0], "high": [200.0], "low": [100.0], "close": [200.0],
        "volume": [1.0],
    })
    df_4h = pd.concat([flat, breakout], ignore_index=True)

    strat = DonchianStrategy(donchian_entry=20, donchian_exit=10, atr_period=14)
    sigs = replay_signals(strat, df_4h, df_daily, "BTC/USD")
    assert sigs == [], (
        f"expected no signal (daily downtrend), got {sigs!r}. "
        "In-progress daily bar may be leaking into the EMA filter."
    )


def test_replay_signals_alternates_buy_then_sell():
    """In replay, a SELL cannot fire before a BUY: position state gates exits."""
    # Synthetic 4h series: flat 100 for 30 bars, breakout to 110, flat for 10 bars,
    # then a crash to 80.
    n_flat = 30
    df = _flat_4h(n_flat, 100.0)
    ts_after = df["ts"].iloc[-1]
    extras = []
    extras.append((ts_after + pd.Timedelta("4h"), 110.0, 100.0, 110.0))  # breakout
    # Hold above the BUY stop (stop = entry - 2*ATR; ATR is ~0 from flat warmup,
    # so the synthetic stop sits right at entry — keep close clearly above 110).
    for k in range(10):
        extras.append((ts_after + pd.Timedelta(f"{4*(k+2)}h"), 116.0, 114.0, 115.0))
    extras.append((ts_after + pd.Timedelta(f"{4*12}h"), 90.0, 80.0, 80.0))  # crash
    tail = pd.DataFrame(extras, columns=["ts", "high", "low", "close"])
    tail["open"] = tail["close"]
    tail["volume"] = 1.0
    df = pd.concat([df, tail[["ts", "open", "high", "low", "close", "volume"]]],
                   ignore_index=True)

    strat = DonchianStrategy(donchian_entry=20, donchian_exit=10, atr_period=14)
    sigs = replay_signals(strat, df, _uptrend_daily(), "BTC/USD")

    sides = [s.side for s in sigs]
    assert sides[:2] == ["BUY", "SELL"], f"unexpected sequence: {sides}"


# ---------- script mode: real signals over recent history ----------

def _load_4h_and_daily_bars(symbol: str, days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prefer SQL Server (Session 4 backfill); else live-fetch from Alpaca."""
    try:
        from db.repository import Repository
        repo = Repository()
        sql = (
            "SELECT ts, open_px AS [open], high_px AS high, low_px AS low, "
            "close_px AS [close], volume FROM market_data "
            "WHERE symbol = ? AND timeframe = ? ORDER BY ts;"
        )
        df_4h = repo.db.query_df(sql, (symbol, "4Hour"))
        df_daily = repo.db.query_df(sql, (symbol, "1Day"))
        if not df_4h.empty and not df_daily.empty:
            df_4h["ts"] = pd.to_datetime(df_4h["ts"], utc=True)
            df_daily["ts"] = pd.to_datetime(df_daily["ts"], utc=True)
            return df_4h, df_daily
    except Exception as e:
        print(f"(SQL load failed: {e!r}; falling back to Alpaca)")

    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from data.alpaca_data import AlpacaDataClient
    client = AlpacaDataClient()
    end = datetime.now(timezone.utc)
    start_4h = end - timedelta(days=days)
    start_daily = end - timedelta(days=max(days, 260))  # need 200+ for EMA
    df_4h = client.get_bars_range(symbol, TimeFrame(4, TimeFrameUnit.Hour),
                                   start_4h, end)
    df_daily = client.get_bars_range(symbol, TimeFrame(1, TimeFrameUnit.Day),
                                      start_daily, end)
    return df_4h, df_daily


if __name__ == "__main__":
    SYMBOL = os.getenv("REPLAY_SYMBOL", "BTC/USD")
    DAYS = int(os.getenv("REPLAY_DAYS", "180"))

    strat = DonchianStrategy.from_yaml("config/config.yaml")
    df_4h, df_daily = _load_4h_and_daily_bars(SYMBOL, DAYS)
    print(f"Loaded {len(df_4h)} 4h bars and {len(df_daily)} daily bars for {SYMBOL}.")
    if df_4h.empty or df_daily.empty:
        raise SystemExit("no data; cannot replay")

    # Walk forward and print each signal alongside its triggering 4h timestamp.
    # (replay_signals returns Signals only; we re-walk here to attach a ts.)
    # Daily-slice rule mirrors replay_signals: a daily bar is only usable once
    # its close has been observed (daily.ts + 1d <= 4h_close = 4h.ts + 4h).
    position = None
    warmup = max(strat.donchian_entry, strat.donchian_exit, strat.atr_period) + 1
    one_day = pd.Timedelta("1D")
    four_h = pd.Timedelta("4h")
    sig_count = 0
    for i in range(warmup, len(df_4h)):
        slice_4h = df_4h.iloc[: i + 1]
        cutoff = slice_4h["ts"].iloc[-1]
        slice_daily = df_daily[df_daily["ts"] + one_day <= cutoff + four_h]
        sig = strat.generate_signal(slice_4h, slice_daily, position, SYMBOL)
        if sig is None:
            continue
        ts_str = pd.Timestamp(cutoff).strftime("%Y-%m-%d %H:%M")
        stop_str = f"stop={sig.stop:.2f}" if sig.stop is not None else "stop=-"
        print(f"  {ts_str}  {sig.side:4s}  px={sig.price:>10.2f}  "
              f"atr={sig.atr:>8.2f}  {stop_str}   {sig.reason}")
        position = {"current_stop": sig.stop} if sig.side == "BUY" else None
        sig_count += 1
    print(f"\nTotal: {sig_count} signals over {len(df_4h)} 4h bars.")
