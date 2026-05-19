"""Backtest smoke test + script-mode runner.

pytest mode uses a synthetic OHLCV series so the backtest can be exercised
without SQL Server or Alpaca — verifies the pipeline wires together, metrics
are finite, and the equity curve PNG lands on disk.

    python -m pytest tests/test_backtest.py    # pytest mode
    python -m tests.test_backtest              # script: runs against SQL Server

The script mode mirrors test_strategy.py's pattern: prefer SQL Server bars,
fall back to live Alpaca fetch if the DB is empty/unreachable. Used by the
user to manually inspect equity curves and metrics.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.runner import BacktestMetrics, run_backtest


# ---------- synthetic fixtures ----------

def _synthetic_uptrend(n_4h: int = 2200, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build a noisy uptrend so the strategy actually generates trades.

    Returns (df_4h, df_daily) with tz-aware UTC ts. The daily frame extends
    260 days before the 4h start so the 200-EMA is warmed up.
    """
    rng = np.random.default_rng(seed)
    ts_4h = pd.date_range("2024-01-01", periods=n_4h, freq="4h", tz="UTC")
    drift = np.linspace(0, 0.6, n_4h)  # ~60% total drift
    noise = rng.normal(0, 0.012, n_4h)
    log_ret = drift / n_4h + noise
    close = 100.0 * np.exp(np.cumsum(log_ret))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n_4h)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n_4h)))
    df_4h = pd.DataFrame({
        "ts": ts_4h, "open": close, "high": high, "low": low, "close": close,
        "volume": 1.0,
    })

    # Daily frame: 260-day warmup + same span as 4h.
    daily_start = ts_4h[0] - pd.Timedelta(days=260)
    daily_end = ts_4h[-1]
    ts_d = pd.date_range(daily_start, daily_end, freq="D", tz="UTC")
    # Smooth uptrend (so 200-EMA filter passes most of the time).
    d_close = 100.0 * np.exp(np.linspace(0, 0.5, len(ts_d)))
    df_daily = pd.DataFrame({
        "ts": ts_d, "open": d_close, "high": d_close * 1.005,
        "low": d_close * 0.995, "close": d_close, "volume": 1.0,
    })
    return df_4h, df_daily


# ---------- pytest ----------

def test_run_backtest_produces_finite_metrics(tmp_path, monkeypatch):
    df_4h, df_daily = _synthetic_uptrend()
    # Redirect the PNG output so we don't pollute the repo's backtest/results/.
    import backtest.runner as runner
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)

    metrics, pf = run_backtest(
        symbol="SYNTH/USD",
        initial_cash=10_000.0,
        df_4h=df_4h,
        df_daily=df_daily,
        save_plot=True,
    )

    assert isinstance(metrics, BacktestMetrics)
    assert metrics.bars == len(df_4h)
    assert metrics.n_trades >= 1, "uptrend should generate at least one trade"
    # Sanity: finite metrics on all numeric fields except profit_factor which
    # may legitimately be inf if there are zero losses.
    finite_fields = (
        metrics.total_return_pct, metrics.cagr_pct, metrics.sharpe,
        metrics.sortino, metrics.max_drawdown_pct, metrics.win_rate_pct,
        metrics.final_equity,
    )
    for v in finite_fields:
        assert math.isfinite(v), f"non-finite metric: {v}"
    assert 0.0 <= metrics.win_rate_pct <= 100.0
    assert metrics.max_drawdown_pct >= 0.0
    assert metrics.final_equity > 0

    # Equity curve PNG was written.
    pngs = list(Path(tmp_path).glob("SYNTHUSD_*.png"))
    assert len(pngs) == 1, f"expected one PNG, got {pngs}"


def test_daily_filter_alignment_blocks_pre_warmup_entries():
    """No entries should fire until the daily 200-EMA has enough history."""
    df_4h, df_daily = _synthetic_uptrend(n_4h=400)
    # Truncate daily to exactly 199 bars before the 4h start → EMA never warms.
    first_4h = df_4h["ts"].iloc[0]
    df_daily_truncated = df_daily[df_daily["ts"] < first_4h].tail(199)

    metrics, _ = run_backtest(
        symbol="SYNTH/USD",
        initial_cash=10_000.0,
        df_4h=df_4h,
        df_daily=df_daily_truncated,
        save_plot=False,
    )
    # With <200 daily bars the filter is never True → no trades.
    assert metrics.n_trades == 0, (
        f"expected 0 trades with insufficient daily warmup, got {metrics.n_trades}"
    )


# ---------- script mode: real-data backtest against SQL Server ----------

def _load_from_sql(symbol: str, days: int) -> tuple[pd.DataFrame, pd.DataFrame] | None:
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
        if df_4h.empty or df_daily.empty:
            return None
        df_4h["ts"] = pd.to_datetime(df_4h["ts"], utc=True)
        df_daily["ts"] = pd.to_datetime(df_daily["ts"], utc=True)
        # Trim 4h to last `days`; keep all daily for EMA warmup.
        cutoff = df_4h["ts"].max() - pd.Timedelta(days=days)
        df_4h = df_4h[df_4h["ts"] >= cutoff].reset_index(drop=True)
        return df_4h, df_daily
    except Exception as e:
        print(f"(SQL load failed: {e!r})")
        return None


if __name__ == "__main__":
    from backtest.runner import print_metrics

    SYMBOL = os.getenv("BACKTEST_SYMBOL", "BTC/USD")
    DAYS = int(os.getenv("BACKTEST_DAYS", "365"))
    CASH = float(os.getenv("BACKTEST_CASH", "10000"))

    loaded = _load_from_sql(SYMBOL, DAYS)
    if loaded is None:
        raise SystemExit(
            f"No bars in SQL Server for {SYMBOL}. Run "
            f"`python -m data.ingest --symbol {SYMBOL} --timeframe 4Hour --days {DAYS}` first."
        )
    df_4h, df_daily = loaded
    print(f"Loaded {len(df_4h)} 4h bars (last {DAYS}d) and {len(df_daily)} daily bars "
          f"for {SYMBOL}.")

    metrics, _ = run_backtest(
        symbol=SYMBOL,
        initial_cash=CASH,
        df_4h=df_4h,
        df_daily=df_daily,
        save_plot=True,
    )
    print_metrics(metrics)
    safe = SYMBOL.replace("/", "")
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    print(f"  Equity curve PNG  : backtest/results/{safe}_{date_tag}.png")
