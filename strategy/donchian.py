"""Donchian-channel breakout strategy (long-only, crypto-adapted Turtle-lite).

Per initial.md §2:
- 4h bars: 20-bar Donchian high (entry), 10-bar Donchian low (trailing exit)
- daily bars: 200-EMA regime filter
- 14-period Wilder's ATR for stops and position sizing
- All channel/ATR values SHIFTED BY 1 BAR before signal evaluation to avoid
  look-ahead bias (using bar t's high in the channel that decides bar t's signal
  is the most common quant bug — strictly forbidden).

This module is pure functions plus one config-holding class. No I/O, no DB,
no broker access — those live in execution/ and risk/.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    price: float
    stop: float | None
    atr: float | None
    reason: str


class DonchianStrategy:
    def __init__(
        self,
        donchian_entry: int = 20,
        donchian_exit: int = 10,
        atr_period: int = 14,
        atr_stop_mult: float = 2.0,
        daily_ema_period: int = 200,
    ) -> None:
        self.donchian_entry = donchian_entry
        self.donchian_exit = donchian_exit
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.daily_ema_period = daily_ema_period

    @classmethod
    def from_yaml(cls, path: str | Path) -> "DonchianStrategy":
        with Path(path).open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        s = cfg.get("strategy", {})
        return cls(
            donchian_entry=int(s.get("donchian_entry", 20)),
            donchian_exit=int(s.get("donchian_exit", 10)),
            atr_period=int(s.get("atr_period", 14)),
            atr_stop_mult=float(s.get("atr_stop_mult", 2.0)),
        )

    # ---------- indicators ----------

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add donchian_high, donchian_low, atr — each shifted by 1 bar.

        After shift, the value on row t reflects only bars strictly before t, so
        comparing `df.close[t]` against `df.donchian_high[t]` is causally clean.
        """
        required = {"high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"compute_indicators: missing columns {sorted(missing)}")

        out = df.copy()
        out["donchian_high"] = (
            out["high"].rolling(self.donchian_entry).max().shift(1)
        )
        out["donchian_low"] = (
            out["low"].rolling(self.donchian_exit).min().shift(1)
        )
        out["atr"] = _wilder_atr(out, self.atr_period).shift(1)
        return out

    def daily_trend_ok(self, df_daily: pd.DataFrame) -> bool:
        """True iff the most recent daily close is above the 200-EMA.

        Assumes the caller has already excluded any in-progress daily bar — see
        replay_signals for the slicing rule. In live use, the 4h scheduler must
        pass only daily bars whose close has been finalized.
        """
        if len(df_daily) < self.daily_ema_period:
            return False
        ema = df_daily["close"].ewm(span=self.daily_ema_period, adjust=False).mean()
        return bool(df_daily["close"].iloc[-1] > ema.iloc[-1])

    # ---------- signal ----------

    def generate_signal(
        self,
        df_4h: pd.DataFrame,
        df_daily: pd.DataFrame,
        current_position: dict | None,
        symbol: str,
    ) -> Signal | None:
        """Decide BUY / SELL / no-op based on the most recent CLOSED 4h bar.

        current_position: None when flat, else {'current_stop': float, ...}.
        Caller is responsible for passing only closed bars — this function does
        not know whether `df_4h.iloc[-1]` is in-progress.
        """
        ind = self.compute_indicators(df_4h)
        if ind.empty:
            return None
        last = ind.iloc[-1]
        if pd.isna(last["donchian_high"]) or pd.isna(last["donchian_low"]) or pd.isna(last["atr"]):
            return None  # indicators not yet warmed up

        close = float(last["close"])
        atr = float(last["atr"])
        dh = float(last["donchian_high"])
        dl = float(last["donchian_low"])

        if current_position is None:
            if close > dh and self.daily_trend_ok(df_daily):
                stop = close - self.atr_stop_mult * atr
                return Signal(
                    symbol=symbol,
                    side="BUY",
                    price=close,
                    stop=stop,
                    atr=atr,
                    reason=f"breakout: close {close:.2f} > donchian_high {dh:.2f} & daily>200EMA",
                )
            return None

        # In position — check trailing exit and hard stop.
        if close < dl:
            return Signal(
                symbol=symbol, side="SELL", price=close, stop=None, atr=atr,
                reason=f"trail exit: close {close:.2f} < donchian_low {dl:.2f}",
            )
        current_stop = current_position.get("current_stop")
        if current_stop is not None and close < float(current_stop):
            return Signal(
                symbol=symbol, side="SELL", price=close, stop=None, atr=atr,
                reason=f"stop hit: close {close:.2f} < current_stop {float(current_stop):.2f}",
            )
        return None


# ---------- helpers ----------

def _wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's ATR == EMA of true range with alpha = 1/period."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def replay_signals(
    strategy: DonchianStrategy,
    df_4h: pd.DataFrame,
    df_daily: pd.DataFrame,
    symbol: str,
) -> list[Signal]:
    """Walk forward through df_4h, emit the Signals that would have fired live.

    Tracks a synthetic flat/long position (no fills, no slippage, no PnL — that's
    the backtest module's job in session 6). Stop is tracked from the BUY signal
    so SELL-on-stop can fire. df_daily is sliced at each step so the daily EMA
    only sees bars whose close was finalized by the time the 4h bar closed:
    bar ts is bar-start, so the 4h bar closes at ts + 4h and a daily bar's
    close is known at ts + 1 day. Using `daily.ts <= 4h.ts` (the naive slice)
    would peek at an in-progress daily close on every 4h bar of the same UTC day.
    """
    if "ts" not in df_4h.columns or "ts" not in df_daily.columns:
        raise ValueError("replay_signals requires a 'ts' column in both frames")

    warmup = max(strategy.donchian_entry, strategy.donchian_exit, strategy.atr_period) + 1
    out: list[Signal] = []
    position: dict | None = None
    one_day = pd.Timedelta("1D")
    four_h = pd.Timedelta("4h")

    for i in range(warmup, len(df_4h)):
        slice_4h = df_4h.iloc[: i + 1]
        cutoff = slice_4h["ts"].iloc[-1]
        slice_daily = df_daily[df_daily["ts"] + one_day <= cutoff + four_h]

        sig = strategy.generate_signal(slice_4h, slice_daily, position, symbol)
        if sig is None:
            continue
        out.append(sig)
        if sig.side == "BUY":
            position = {"current_stop": sig.stop}
        else:
            position = None
    return out
