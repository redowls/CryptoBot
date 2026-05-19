"""RiskManager: caps, daily-loss halt, drawdown kill, stop monitor.

Per initial.md §6 Session 8, this module enforces:
  1. max_positions cap        — open count ≥ cap → skip new entry
  2. max_daily_loss_pct       — equity vs today's anchor ≤ -cap → halt entries
                                until next UTC day
  3. max_drawdown_pct         — equity vs peak ≤ -cap → KILL SWITCH (close all
                                positions, request shutdown)
  4. volatility position sizing — wraps risk.sizing.compute_position_qty

It also owns the 1-minute stop monitor: for each open position with a stored
`current_stop`, fetch Alpaca's latest bid; if bid ≤ stop, fire a market SELL,
record a CLOSED `trades` row with realised PnL, and clear the SQL position
(reconcile re-syncs from Alpaca on the next 4h tick).

Design choices worth preserving:
- **Drawdown beats daily loss.** When both gates trip, the manager reports
  KILL_SWITCH so the caller does the irreversible thing (close all) rather than
  the soft thing (skip new entries).
- **Daily anchor falls back across the midnight boundary.** If no snapshot has
  been taken yet today, we use the most recent snapshot from before 00:00 UTC.
  Otherwise a fresh bot start would have no anchor and entries would never halt.
- **Stops are compared against bid_price.** That's where a market SELL of a
  long would actually clear, so it matches realised slippage. `ask_price` is
  the fallback if the SDK returns no bid.
- **Position is deleted, not zeroed, after a stop exit.** The 4h reconcile is
  authoritative; if the SELL is still pending when reconcile runs, the row
  comes back without a stop and is harmless until risk re-attaches one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from risk.sizing import compute_position_qty

log = logging.getLogger(__name__)


class RiskAction(str, Enum):
    ALLOW = "allow"
    HALT_MAX_POSITIONS = "halt_max_positions"
    HALT_DAILY_LOSS = "halt_daily_loss"
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == RiskAction.ALLOW


class RiskManager:
    def __init__(
        self,
        *,
        repo,
        executor,
        data_client,
        max_positions: int = 2,
        max_daily_loss_pct: float = 0.03,
        max_drawdown_pct: float = 0.15,
        risk_per_trade: float = 0.01,
        atr_stop_mult: float = 2.0,
        max_pct_equity: float = 0.25,
        notify_fn: Callable[[str], None] | None = None,
        on_kill_switch: Callable[[], None] | None = None,
    ) -> None:
        self.repo = repo
        self.executor = executor
        self.data_client = data_client
        self.max_positions = int(max_positions)
        self.max_daily_loss_pct = float(max_daily_loss_pct)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.risk_per_trade = float(risk_per_trade)
        self.atr_stop_mult = float(atr_stop_mult)
        self.max_pct_equity = float(max_pct_equity)
        self.notify_fn = notify_fn
        self.on_kill_switch = on_kill_switch
        self._kill_fired = False

    @classmethod
    def from_config(
        cls,
        *,
        repo,
        executor,
        data_client,
        risk_cfg: dict,
        strategy_cfg: dict,
        notify_fn: Callable[[str], None] | None = None,
        on_kill_switch: Callable[[], None] | None = None,
    ) -> "RiskManager":
        return cls(
            repo=repo,
            executor=executor,
            data_client=data_client,
            max_positions=int(risk_cfg.get("max_positions", 2)),
            max_daily_loss_pct=float(risk_cfg.get("max_daily_loss_pct", 0.03)),
            max_drawdown_pct=float(risk_cfg.get("max_drawdown_pct", 0.15)),
            risk_per_trade=float(risk_cfg.get("risk_per_trade", 0.01)),
            atr_stop_mult=float(strategy_cfg.get("atr_stop_mult", 2.0)),
            max_pct_equity=float(risk_cfg.get("max_pct_equity", 0.25)),
            notify_fn=notify_fn,
            on_kill_switch=on_kill_switch,
        )

    # ---------- gates ----------

    def check_drawdown(self, current_equity: float) -> RiskDecision:
        peak = self.repo.get_peak_equity()
        if peak is None or peak <= 0:
            return RiskDecision(RiskAction.ALLOW)
        dd = (current_equity - peak) / peak
        if dd <= -self.max_drawdown_pct:
            return RiskDecision(
                RiskAction.KILL_SWITCH,
                f"drawdown {dd:.2%} from peak ${peak:,.2f} "
                f"breaches -{self.max_drawdown_pct:.0%}",
            )
        return RiskDecision(RiskAction.ALLOW)

    def check_daily_loss(self, current_equity: float) -> RiskDecision:
        anchor = self.repo.get_daily_anchor_equity()
        if anchor is None or anchor <= 0:
            return RiskDecision(RiskAction.ALLOW)
        change = (current_equity - anchor) / anchor
        if change <= -self.max_daily_loss_pct:
            return RiskDecision(
                RiskAction.HALT_DAILY_LOSS,
                f"daily P&L {change:.2%} from anchor ${anchor:,.2f} "
                f"breaches -{self.max_daily_loss_pct:.0%}",
            )
        return RiskDecision(RiskAction.ALLOW)

    def gate_new_entry(
        self,
        *,
        current_equity: float,
        open_positions_count: int,
    ) -> RiskDecision:
        """Returns the first failing gate, or ALLOW.

        Drawdown is checked before daily loss because KILL_SWITCH is more severe
        than HALT_DAILY_LOSS — if both trip on the same equity reading, we want
        the caller to act on the irreversible one.
        """
        dd = self.check_drawdown(current_equity)
        if not dd.allowed:
            return dd
        dl = self.check_daily_loss(current_equity)
        if not dl.allowed:
            return dl
        if open_positions_count >= self.max_positions:
            return RiskDecision(
                RiskAction.HALT_MAX_POSITIONS,
                f"{open_positions_count} open positions ≥ cap {self.max_positions}",
            )
        return RiskDecision(RiskAction.ALLOW)

    # ---------- sizing ----------

    def position_size(self, *, equity: float, atr: float, price: float) -> float:
        return compute_position_qty(
            equity=equity,
            atr=atr,
            price=price,
            risk_per_trade=self.risk_per_trade,
            atr_stop_mult=self.atr_stop_mult,
            max_pct_equity=self.max_pct_equity,
        )

    # ---------- kill switch ----------

    def trigger_kill_switch(self, reason: str) -> int:
        """Close every open position and signal shutdown. Idempotent: a second
        call after the first is a no-op (returns 0). Returns the number of
        positions for which a SELL was successfully submitted."""
        if self._kill_fired:
            log.warning("kill switch re-entrancy ignored (already fired)")
            return 0
        self._kill_fired = True

        log.critical("KILL SWITCH: %s", reason)
        try:
            self.repo.log("ERROR", "risk.manager", f"KILL SWITCH: {reason}")
        except Exception:
            log.exception("failed to persist kill-switch log row")

        if self.notify_fn:
            try:
                self.notify_fn(
                    f"KILL SWITCH TRIGGERED\n{reason}\n"
                    f"Closing all positions and stopping bot."
                )
            except Exception:
                log.exception("notify failed during kill switch")

        closed = 0
        try:
            positions = self.repo.get_open_positions_with_stops()
        except Exception:
            log.exception("failed to load positions during kill switch")
            positions = []

        for p in positions:
            symbol = p["symbol"]
            qty = float(p["qty"])
            if qty <= 0:
                continue
            try:
                self.executor.submit_market_order(symbol=symbol, qty=qty, side="SELL")
                closed += 1
            except Exception:
                log.exception("kill switch SELL failed for %s", symbol)

        if self.on_kill_switch:
            try:
                self.on_kill_switch()
            except Exception:
                log.exception("on_kill_switch callback raised")
        return closed

    # ---------- stop monitor (1-minute job) ----------

    def monitor_stops(self) -> list[dict]:
        """For each position with a stored stop, sell if latest bid ≤ stop.

        Returns one dict per triggered exit so callers (or tests) can inspect:
            {"symbol", "qty", "exit_price", "stop"}
        """
        triggered: list[dict] = []

        try:
            positions = self.repo.get_open_positions_with_stops()
        except Exception:
            log.exception("monitor_stops: failed to load positions")
            return triggered

        for p in positions:
            symbol = p["symbol"]
            stop = p["current_stop"]
            qty = float(p["qty"])
            if stop is None or qty <= 0:
                continue

            try:
                quote = self.data_client.get_latest_quote(symbol)
            except Exception as e:
                log.exception("monitor_stops: latest quote failed for %s", symbol)
                self.repo.log(
                    "ERROR", "risk.manager.monitor_stops",
                    f"{symbol} latest quote failed", exc=e,
                )
                continue

            price = _quote_price(quote)
            if price is None or price <= 0:
                log.warning("monitor_stops: unusable quote for %s: %r", symbol, quote)
                continue
            if price > float(stop):
                continue  # stop not breached

            log.warning(
                "stop triggered: %s bid=%.4f stop=%.4f qty=%s",
                symbol, price, float(stop), qty,
            )

            try:
                order = self.executor.submit_market_order(
                    symbol=symbol, qty=qty, side="SELL",
                )
            except Exception as e:
                log.exception("monitor_stops: SELL failed for %s", symbol)
                self.repo.log(
                    "ERROR", "risk.manager.monitor_stops",
                    f"{symbol} stop SELL failed", exc=e,
                )
                continue

            raw = getattr(order, "raw", {}) or {}
            fill_px = _nonzero_float(raw.get("filled_avg_price"))
            exit_price = fill_px if fill_px is not None else price
            exit_ts = datetime.now(timezone.utc)
            entry_price = float(p["avg_entry_price"])
            entry_ts = p["opened_at"] or exit_ts

            try:
                self.repo.record_closed_trade(
                    symbol=symbol,
                    qty=qty,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                )
            except Exception:
                log.exception("monitor_stops: record_closed_trade failed for %s", symbol)

            try:
                self.repo.delete_position(symbol)
            except Exception:
                log.exception("monitor_stops: delete_position failed for %s", symbol)

            pnl = (exit_price - entry_price) * qty
            if self.notify_fn:
                try:
                    self.notify_fn(
                        f"STOP HIT {symbol}\n"
                        f"sold qty={qty:.6f} @ ~{exit_price:.2f} "
                        f"(stop={float(stop):.2f})\n"
                        f"pnl=${pnl:,.2f}"
                    )
                except Exception:
                    log.exception("notify failed for stop hit on %s", symbol)

            triggered.append({
                "symbol": symbol,
                "qty": qty,
                "exit_price": exit_price,
                "stop": float(stop),
                "pnl_usd": pnl,
            })

        return triggered


# ---------- helpers ----------

def _quote_price(quote) -> float | None:
    """Prefer bid (what a long SELL clears at); fall back to ask, then mid."""
    if quote is None:
        return None
    bid = _nonzero_float(getattr(quote, "bid_price", None))
    if bid is not None:
        return bid
    ask = _nonzero_float(getattr(quote, "ask_price", None))
    if ask is not None:
        return ask
    return None


def _nonzero_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
