"""Vectorized Donchian-channel backtest using vectorbt.

Reuses [strategy/donchian.py](../strategy/donchian.py) `DonchianStrategy.compute_indicators`
so the backtest and the live signal path can't drift on indicator math (the
shift-by-1 look-ahead guard, in particular, lives in exactly one place).

Pipeline:
  1. Pull 4h + daily bars from SQL Server. Daily fetch starts ~260 days before
     `start_date` so the 200-EMA is warmed up at the first 4h decision.
  2. Compute Donchian high/low + Wilder ATR via the strategy module.
  3. Align the daily 200-EMA filter onto the 4h index, gated by close-availability
     (a daily bar is only usable once its close has been observed).
  4. Build boolean `entries`/`exits` and ATR-derived `sl_stop` + per-entry size
     and hand them to vectorbt's `Portfolio.from_signals`.

Stop semantics: matches [strategy/donchian.py](../strategy/donchian.py) exactly —
both the ATR-based hard stop and the 10-bar Donchian-low trailing exit fire on a
4h CLOSE below the level, never intra-bar. We do not use vectorbt's `sl_stop`
(which triggers intra-bar via high/low) because the live execution path is
close-based; using vb's stop here would silently inflate backtest performance
versus what the live bot would actually do.

CLI:
    python -m backtest.runner --symbol BTC/USD --start 2022-01-01 --end 2025-01-01
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import vectorbt as vbt
from dotenv import load_dotenv

from strategy.donchian import DonchianStrategy

load_dotenv()

RESULTS_DIR = Path(__file__).parent / "results"
EMA_WARMUP_DAYS = 260  # > 200 to warm the daily 200-EMA before start_date


@dataclass
class BacktestMetrics:
    symbol: str
    start: str
    end: str
    bars: int
    n_trades: int
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    avg_win_loss_ratio: float
    initial_cash: float
    final_equity: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_backtest(
    symbol: str,
    start_date: str | datetime | None = None,
    end_date: str | datetime | None = None,
    initial_cash: float = 10_000.0,
    fee_pct: float = 0.0025,
    slippage_pct: float = 0.0005,
    risk_per_trade: float = 0.01,
    max_pos_pct: float = 0.25,
    config_path: str | Path = "config/config.yaml",
    save_plot: bool = True,
    df_4h: pd.DataFrame | None = None,
    df_daily: pd.DataFrame | None = None,
) -> tuple[BacktestMetrics, "vbt.Portfolio"]:
    """Run a Donchian-channel backtest for one symbol over a date range.

    df_4h / df_daily are optional injection points for tests — when provided,
    SQL is skipped. Both must already have a tz-aware UTC `ts` column and the
    standard OHLCV columns. The daily frame should include ~260 days of warmup
    before the 4h range.
    """
    start = pd.Timestamp(start_date, tz="UTC") if start_date is not None else None
    end = pd.Timestamp(end_date, tz="UTC") if end_date is not None else None
    strat = DonchianStrategy.from_yaml(config_path)

    if df_4h is None or df_daily is None:
        daily_buffer = (
            (start - pd.Timedelta(days=EMA_WARMUP_DAYS)).to_pydatetime()
            if start is not None
            else None
        )
        df_4h = _load_bars(
            symbol, "4Hour",
            start.to_pydatetime() if start is not None else None,
            end.to_pydatetime() if end is not None else None,
        )
        df_daily = _load_bars(
            symbol, "1Day",
            daily_buffer,
            end.to_pydatetime() if end is not None else None,
        )

    if df_4h.empty or df_daily.empty:
        raise ValueError(
            f"no bars for {symbol} in range {start_date}..{end_date}; "
            f"run `python -m data.ingest --symbol {symbol} ...` first"
        )

    df_4h = df_4h.sort_values("ts").reset_index(drop=True)
    df_daily = df_daily.sort_values("ts").reset_index(drop=True)

    ind = strat.compute_indicators(df_4h)
    close = ind["close"].astype(float)
    dh = ind["donchian_high"]
    dl = ind["donchian_low"]
    atr = ind["atr"]

    daily_ok = _daily_filter_at_4h(df_4h, df_daily, strat.daily_ema_period)

    entries, exits = _walk_close_based_signals(
        close=close, dh=dh, dl=dl, atr=atr, daily_ok=daily_ok,
        atr_stop_mult=strat.atr_stop_mult,
    )

    # Size: only the value at entry bars matters. risk_per_trade / stop_pct,
    # capped at max_pos_pct, using ATR at the entry bar.
    stop_pct = (strat.atr_stop_mult * atr / close).clip(lower=1e-6).fillna(0.0)
    size_frac = (risk_per_trade / stop_pct.replace(0.0, np.nan)).clip(upper=max_pos_pct).fillna(0.0)

    idx = pd.DatetimeIndex(df_4h["ts"])
    close.index = idx
    entries.index = idx
    exits.index = idx
    size_frac.index = idx

    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=entries.values,
        exits=exits.values,
        size=size_frac.values,
        size_type="percent",
        init_cash=initial_cash,
        fees=fee_pct,
        slippage=slippage_pct,
        freq="4h",
        direction="longonly",
    )

    metrics = _summarize(pf, symbol, df_4h, initial_cash)
    if save_plot:
        _plot_equity(pf, symbol, metrics)
    return metrics, pf


def _load_bars(
    symbol: str,
    timeframe: str,
    start: datetime | None,
    end: datetime | None,
) -> pd.DataFrame:
    from db.repository import Repository  # lazy: don't import DB at module load

    repo = Repository()
    sql = (
        "SELECT ts, open_px AS [open], high_px AS high, low_px AS low, "
        "close_px AS [close], volume FROM market_data "
        "WHERE symbol = ? AND timeframe = ?"
    )
    params: list[Any] = [symbol, timeframe]
    if start is not None:
        sql += " AND ts >= ?"
        params.append(start.replace(tzinfo=None) if start.tzinfo else start)
    if end is not None:
        sql += " AND ts < ?"
        params.append(end.replace(tzinfo=None) if end.tzinfo else end)
    sql += " ORDER BY ts;"

    df = repo.db.query_df(sql, tuple(params))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def _daily_filter_at_4h(
    df_4h: pd.DataFrame, df_daily: pd.DataFrame, ema_period: int
) -> pd.Series:
    """Align the daily 200-EMA "close > EMA" boolean onto the 4h bar index,
    respecting daily-close availability.

    A daily bar with start ts=D is finalized at D + 1d. A 4h bar with start
    ts=T closes at T + 4h. So at the 4h decision moment T + 4h, the most
    recent usable daily has D + 1d <= T + 4h, i.e. D <= T - 20h. We shift the
    daily index by +1d to its "available-from" time, then reindex onto the 4h
    close times with ffill — bars before the first available daily fall back
    to False so we never fire entries during EMA warmup.
    """
    daily = df_daily.set_index("ts").sort_index()
    ema = daily["close"].ewm(span=ema_period, adjust=False).mean()
    pass_ = (daily["close"] > ema).astype(bool)
    # Cumulative count of daily bars visible at each "available-from" time.
    # Matches strategy.daily_trend_ok's `len(df_daily) < ema_period -> False` gate.
    counts = pd.Series(np.arange(1, len(daily) + 1), index=daily.index)
    shifted = pass_.copy()
    shifted.index = shifted.index + pd.Timedelta("1D")
    counts.index = counts.index + pd.Timedelta("1D")

    ts_close = pd.DatetimeIndex(df_4h["ts"]) + pd.Timedelta("4h")
    aligned = shifted.reindex(ts_close, method="ffill")
    n_visible = counts.reindex(ts_close, method="ffill").fillna(0)
    warmed_up = (n_visible >= ema_period).values
    return (
        pd.Series(aligned.values, index=df_4h.index).fillna(False).astype(bool)
        & pd.Series(warmed_up, index=df_4h.index)
    )


def _walk_close_based_signals(
    close: pd.Series,
    dh: pd.Series,
    dl: pd.Series,
    atr: pd.Series,
    daily_ok: pd.Series,
    atr_stop_mult: float,
) -> tuple[pd.Series, pd.Series]:
    """Forward-walk over bars to produce entry/exit booleans with CLOSE-based stops.

    Mirrors [strategy/donchian.py](../strategy/donchian.py) generate_signal exactly:
    entry on close > donchian_high & daily_ok (and flat); exit on close < donchian_low
    OR close < stop_at_entry (where stop_at_entry = close - atr_stop_mult*ATR at the
    entry bar). State (flat/long + stop_at_entry) is tracked across the walk; this is
    O(n) and runs once per backtest so it's not a hot path.
    """
    n = len(close)
    entries = np.zeros(n, dtype=bool)
    exits = np.zeros(n, dtype=bool)
    in_pos = False
    stop_level = 0.0
    close_v = close.values
    dh_v = dh.values
    dl_v = dl.values
    atr_v = atr.values
    daily_v = daily_ok.values
    for i in range(n):
        # Skip warmup bars where any indicator is NaN.
        if np.isnan(dh_v[i]) or np.isnan(dl_v[i]) or np.isnan(atr_v[i]):
            continue
        c = close_v[i]
        if not in_pos:
            if c > dh_v[i] and daily_v[i]:
                entries[i] = True
                in_pos = True
                stop_level = c - atr_stop_mult * atr_v[i]
        else:
            if c < dl_v[i] or c < stop_level:
                exits[i] = True
                in_pos = False
                stop_level = 0.0
    return (
        pd.Series(entries, index=close.index),
        pd.Series(exits, index=close.index),
    )


def _summarize(
    pf: "vbt.Portfolio",
    symbol: str,
    df_4h: pd.DataFrame,
    initial_cash: float,
) -> BacktestMetrics:
    trades_df = pf.trades.records_readable
    n_trades = int(len(trades_df))
    pnl = trades_df["PnL"].values.astype(float) if n_trades else np.array([])
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    win_rate = (len(wins) / n_trades * 100.0) if n_trades else 0.0
    if losses.size and wins.size:
        profit_factor = float(wins.sum() / -losses.sum())
        avg_ratio = float(wins.mean() / -losses.mean())
    elif wins.size:
        profit_factor = float("inf")
        avg_ratio = float("inf")
    else:
        profit_factor = 0.0
        avg_ratio = 0.0

    eq = pf.value()
    final_eq = float(eq.iloc[-1])
    total_return_pct = (final_eq / initial_cash - 1.0) * 100.0
    span_days = (df_4h["ts"].iloc[-1] - df_4h["ts"].iloc[0]).total_seconds() / 86400.0
    years = max(span_days / 365.25, 1e-9)
    cagr_pct = ((final_eq / initial_cash) ** (1 / years) - 1.0) * 100.0

    sharpe = float(_safe_scalar(pf.sharpe_ratio()))
    sortino = float(_safe_scalar(pf.sortino_ratio()))
    max_dd_pct = float(abs(_safe_scalar(pf.max_drawdown())) * 100.0)

    return BacktestMetrics(
        symbol=symbol,
        start=str(df_4h["ts"].iloc[0]),
        end=str(df_4h["ts"].iloc[-1]),
        bars=int(len(df_4h)),
        n_trades=n_trades,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_dd_pct,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        avg_win_loss_ratio=avg_ratio,
        initial_cash=float(initial_cash),
        final_equity=final_eq,
    )


def _safe_scalar(v: Any) -> float:
    if hasattr(v, "item"):
        try:
            return float(v.item())
        except (ValueError, TypeError):
            pass
    if isinstance(v, pd.Series):
        return float(v.iloc[0]) if len(v) else float("nan")
    return float(v)


def _plot_equity(pf: "vbt.Portfolio", symbol: str, m: BacktestMetrics) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    eq = pf.value()
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(eq.index, eq.values, linewidth=1.0)
    ax.set_title(
        f"{symbol} Donchian backtest — "
        f"ret {m.total_return_pct:+.1f}% | CAGR {m.cagr_pct:+.1f}% | "
        f"Sharpe {m.sharpe:.2f} | MaxDD {m.max_drawdown_pct:.1f}% | "
        f"trades {m.n_trades}"
    )
    ax.set_xlabel("UTC")
    ax.set_ylabel("Equity (USD)")
    ax.grid(True, alpha=0.3)
    safe = symbol.replace("/", "")
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = RESULTS_DIR / f"{safe}_{date_tag}.png"
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def print_metrics(m: BacktestMetrics) -> None:
    print(f"\n=== Backtest: {m.symbol}  {m.start[:10]} -> {m.end[:10]}  ({m.bars} bars) ===")
    print(f"  Initial cash      : ${m.initial_cash:,.2f}")
    print(f"  Final equity      : ${m.final_equity:,.2f}")
    print(f"  Total return      : {m.total_return_pct:+.2f}%")
    print(f"  CAGR              : {m.cagr_pct:+.2f}%")
    print(f"  Sharpe ratio      : {m.sharpe:+.3f}")
    print(f"  Sortino ratio     : {m.sortino:+.3f}")
    print(f"  Max drawdown      : {m.max_drawdown_pct:.2f}%")
    print(f"  Trades            : {m.n_trades}")
    print(f"  Win rate          : {m.win_rate_pct:.1f}%")
    print(f"  Profit factor     : {m.profit_factor:.2f}")
    print(f"  Avg win/loss      : {m.avg_win_loss_ratio:.2f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Donchian backtest runner (vectorbt).")
    p.add_argument("--symbol", required=True, help="e.g. BTC/USD")
    p.add_argument("--start", default=None, help="ISO date, e.g. 2022-01-01")
    p.add_argument("--end", default=None, help="ISO date, e.g. 2025-01-01")
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--fee", type=float, default=0.0025)
    p.add_argument("--slippage", type=float, default=0.0005)
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args(argv)

    metrics, _ = run_backtest(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        initial_cash=args.cash,
        fee_pct=args.fee,
        slippage_pct=args.slippage,
        save_plot=not args.no_plot,
    )
    print_metrics(metrics)
    if not args.no_plot:
        safe = args.symbol.replace("/", "")
        date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
        print(f"  Equity curve PNG  : backtest/results/{safe}_{date_tag}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
