"""Bot entrypoint: APScheduler-driven signal cycle on 4h crypto bars.

Per initial.md §3, §6 Session 7:
- BackgroundScheduler runs `signal_cycle` on `0 */4 * * *` UTC.
- Each tick: reconcile SQL positions with Alpaca (Alpaca authoritative), then
  for each configured pair fetch fresh bars, generate a signal, and if one
  fires, persist + submit a market order + notify.
- Session 8 will add the 1-minute stop monitor and 1-hour equity snapshot jobs
  to this same scheduler. Session 9 swaps the telegram stub for the real bot.

Closed-bar discipline: a 4h cron fires at the boundary, but bars dated *now*
are technically just-closed; we still defensively filter `ts + period <= now`
so an in-flight bar can never leak into the signal (the most common quant bug,
per CLAUDE.md "Look-ahead bias" invariant).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
import yaml
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

from data.alpaca_data import AlpacaDataClient
from db.repository import Repository
from execution.alpaca_executor import Executor
from notifications.telegram import (
    TelegramLogHandler,
    send_daily_summary,
    send_message as notify,
)
from risk.manager import RiskAction, RiskManager
from strategy.donchian import DonchianStrategy

CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"
LOG_PATH = Path(__file__).parent / "logs" / "bot.log"

FOUR_HOUR = TimeFrame(amount=4, unit=TimeFrameUnit.Hour)
DAILY = TimeFrame(amount=1, unit=TimeFrameUnit.Day)

log = logging.getLogger("cryptobot")


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging() -> None:
    """Configure file + stdout + Telegram log handlers on the root logger.

    Handlers go on root (not just the 'cryptobot' logger) so messages from
    modules using `logging.getLogger(__name__)` (execution/, risk/, etc.)
    propagate up and reach the rotating file and the Telegram bridge.
    """
    LOG_PATH.parent.mkdir(exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, TelegramLogHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    th = TelegramLogHandler()
    th.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    root.addHandler(th)


class Bot:
    def __init__(
        self,
        config: dict,
        paper: bool = True,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.pairs: list[str] = config["market"]["pairs"]
        self.risk_cfg: dict = config.get("risk", {})
        self.strategy_cfg: dict = config.get("strategy", {})
        self.shutdown_event = shutdown_event or threading.Event()

        self.data_client = AlpacaDataClient(paper=paper)
        self.repo = Repository()
        self.executor = Executor(repo=self.repo, paper=paper)
        self.strategy = DonchianStrategy(
            donchian_entry=int(self.strategy_cfg.get("donchian_entry", 20)),
            donchian_exit=int(self.strategy_cfg.get("donchian_exit", 10)),
            atr_period=int(self.strategy_cfg.get("atr_period", 14)),
            atr_stop_mult=float(self.strategy_cfg.get("atr_stop_mult", 2.0)),
        )
        self.risk = RiskManager.from_config(
            repo=self.repo,
            executor=self.executor,
            data_client=self.data_client,
            risk_cfg=self.risk_cfg,
            strategy_cfg=self.strategy_cfg,
            notify_fn=notify,
            # Kill-switch flips the same event SIGTERM/SIGINT uses, so the main
            # loop exits cleanly and systemd does not see a crash.
            on_kill_switch=self.shutdown_event.set,
        )

    # ---------- per-cycle work ----------

    def signal_cycle(self) -> None:
        """One 4h tick: reconcile, then per-pair signal+execute."""
        try:
            recon = self.executor.reconcile_positions()
            log.info("reconcile: %s", recon)
        except Exception as e:
            log.exception("reconcile failed")
            self.repo.log("ERROR", "main.signal_cycle", "reconcile failed", exc=e)
            return  # don't trade with stale position view

        try:
            account = self.data_client.get_account_info()
            equity = float(account.equity)
            self.repo.insert_snapshot(
                equity=equity,
                cash=float(account.cash),
                buying_power=float(account.buying_power),
                portfolio_value=float(account.portfolio_value),
            )
        except Exception as e:
            log.exception("account fetch failed")
            self.repo.log("ERROR", "main.signal_cycle", "account fetch failed", exc=e)
            return

        for symbol in self.pairs:
            try:
                self._process_symbol(symbol, equity)
            except Exception as e:
                log.exception("symbol %s failed", symbol)
                self.repo.log("ERROR", "main.signal_cycle", f"{symbol} cycle failed", exc=e)

    def _process_symbol(self, symbol: str, equity: float) -> None:
        df_4h = self.data_client.get_bars(symbol, FOUR_HOUR, lookback_days=60)
        df_daily = self.data_client.get_bars(symbol, DAILY, lookback_days=365)

        # Drop in-progress bars (defense in depth — Alpaca usually doesn't
        # return them, but if it does, they would create look-ahead bias).
        now = pd.Timestamp(datetime.now(timezone.utc))
        df_4h = df_4h[df_4h["ts"] + pd.Timedelta("4h") <= now].reset_index(drop=True)
        df_daily = df_daily[df_daily["ts"] + pd.Timedelta("1D") <= now].reset_index(drop=True)

        if df_4h.empty or df_daily.empty:
            log.info("%s: insufficient bars (4h=%d daily=%d), skipping",
                     symbol, len(df_4h), len(df_daily))
            return

        current_position = self._load_position(symbol)
        sig = self.strategy.generate_signal(df_4h, df_daily, current_position, symbol)
        if sig is None:
            log.info("%s: no signal", symbol)
            return

        signal_id = self.repo.insert_signal(
            symbol=sig.symbol,
            strategy=f"donchian_{self.strategy.donchian_entry}_{self.strategy.donchian_exit}",
            side=sig.side,
            signal_price=sig.price,
            atr=sig.atr,
            proposed_stop=sig.stop,
            notes=sig.reason,
        )

        if sig.side == "BUY":
            if current_position is not None:
                log.info("%s: BUY signal but already long; skipping", symbol)
                return

            open_count = self.repo.count_open_positions()
            decision = self.risk.gate_new_entry(
                current_equity=equity, open_positions_count=open_count,
            )
            if decision.action == RiskAction.KILL_SWITCH:
                log.critical("%s: BUY blocked — KILL SWITCH: %s", symbol, decision.reason)
                self.risk.trigger_kill_switch(decision.reason)
                return
            if not decision.allowed:
                log.warning("%s: BUY blocked by risk (%s): %s",
                            symbol, decision.action.value, decision.reason)
                self.repo.log("WARN", "main.signal_cycle",
                              f"{symbol} BUY blocked: {decision.reason}")
                return

            qty = self.risk.position_size(
                equity=equity, atr=float(sig.atr), price=float(sig.price),
            )
        else:  # SELL — exit the full position
            if current_position is None:
                log.info("%s: SELL signal but no position; skipping", symbol)
                return
            qty = float(current_position["qty"])

        if qty <= 0:
            log.warning("%s: computed qty=%s, skipping", symbol, qty)
            return

        if sig.side == "BUY":
            order = self.executor.submit_market_order(
                symbol=symbol, qty=qty, side="BUY", signal_id=signal_id,
            )
        else:
            # SELL exits go through close_position so Alpaca decides the qty
            # from its authoritative balance — sidesteps the DECIMAL(28,8)
            # rounding bug where SQL qty drifts above Alpaca's actual holding
            # by a few satoshis and triggers HTTP 403 / code 40310000.
            order = self.executor.close_position(
                symbol=symbol, qty_hint=qty, signal_id=signal_id,
            )
        log.info("submitted %s %s qty=%s status=%s coid=%s",
                 sig.side, symbol, qty, order.status, order.client_order_id)

        # Pre-create the positions row for BUY so the stop is recorded even
        # before the next reconcile sees Alpaca's fill. Reconcile's COALESCE
        # preserves the stop while overwriting qty/avg_entry with Alpaca's
        # authoritative values once the fill is observed.
        if sig.side == "BUY" and sig.stop is not None:
            self.repo.upsert_position(
                symbol=symbol, qty=qty, avg_entry_price=float(sig.price),
                current_stop=float(sig.stop),
            )

        notify(
            f"{sig.side} {symbol} qty={qty:.6f} @ ~{sig.price:.2f}\n"
            f"Reason: {sig.reason}\n"
            f"Order: {order.client_order_id} status={order.status}"
        )

    # ---------- session 8 jobs ----------

    def stop_monitor(self) -> None:
        """1-minute job: trip stored stops against the latest Alpaca bid.

        Wraps risk.monitor_stops with the same try/except + DB-log pattern as
        signal_cycle so a single quote/SELL failure can't kill the scheduler.
        """
        try:
            triggered = self.risk.monitor_stops()
            if triggered:
                log.warning("stop_monitor exits: %s", triggered)
        except Exception as e:
            log.exception("stop_monitor failed")
            try:
                self.repo.log("ERROR", "main.stop_monitor", "monitor cycle failed", exc=e)
            except Exception:
                log.exception("failed to persist stop_monitor error")

    def equity_snapshot(self) -> None:
        """1-hour job: persist a fresh account snapshot for drawdown / daily-loss
        gates. Also re-checks drawdown post-snapshot — if equity has bled in
        between 4h cycles, the kill switch fires here rather than waiting for
        the next entry attempt."""
        try:
            account = self.data_client.get_account_info()
            equity = float(account.equity)
            self.repo.insert_snapshot(
                equity=equity,
                cash=float(account.cash),
                buying_power=float(account.buying_power),
                portfolio_value=float(account.portfolio_value),
            )
        except Exception as e:
            log.exception("equity_snapshot failed")
            try:
                self.repo.log("ERROR", "main.equity_snapshot", "snapshot failed", exc=e)
            except Exception:
                log.exception("failed to persist equity_snapshot error")
            return

        dd = self.risk.check_drawdown(equity)
        if dd.action == RiskAction.KILL_SWITCH:
            log.critical("equity_snapshot: KILL SWITCH: %s", dd.reason)
            self.risk.trigger_kill_switch(dd.reason)

    def daily_summary(self) -> None:
        """00:00 UTC job: build + Telegram-send the prior-day report.

        Wrapped in its own try/except (rather than letting the scheduler swallow
        it) so the failure lands in the DB log table for later review.
        """
        try:
            send_daily_summary(self.repo)
        except Exception as e:
            log.exception("daily_summary failed")
            try:
                self.repo.log("ERROR", "main.daily_summary", "summary failed", exc=e)
            except Exception:
                log.exception("failed to persist daily_summary error")

    def _load_position(self, symbol: str) -> dict | None:
        sql = (
            "SELECT qty, avg_entry_price, current_stop, opened_at "
            "FROM positions WHERE symbol = ?;"
        )
        df = self.repo.db.query_df(sql, (symbol,))
        if df.empty:
            return None
        r = df.iloc[0]
        cs = r["current_stop"]
        return {
            "qty": float(r["qty"]),
            "avg_entry_price": float(r["avg_entry_price"]),
            "current_stop": float(cs) if cs is not None and not pd.isna(cs) else None,
            "opened_at": r["opened_at"],
        }


# ---------- entrypoint ----------

def main() -> None:
    """Bot entrypoint. Wraps the real work in `_run` so a top-level uncaught
    exception still gets logged to SQL + Telegram before re-raising — which is
    what triggers `systemd Restart=always` to bring the process back up."""
    load_dotenv()
    setup_logging()

    try:
        _run()
    except SystemExit:
        raise
    except BaseException as e:
        log.critical("Uncaught exception in main()", exc_info=True)
        # Best-effort persist + notify. Each step in its own try so a downstream
        # failure (DB unreachable, Telegram down) can't mask the original error.
        # A fresh Repository here is intentional: if _run() died during Bot
        # init, its repo may not exist — a new one is the only handle we have.
        try:
            Repository().log("CRITICAL", "main", "uncaught exception", exc=e)
        except Exception:
            log.exception("failed to persist crash log")
        try:
            notify(f"BOT CRASHED: {type(e).__name__}: {e}")
        except Exception:
            log.exception("failed to send crash Telegram")
        # Re-raise so systemd sees a non-zero exit and restarts us.
        raise


def _run() -> None:
    config = load_config()
    paper = os.getenv("ALPACA_PAPER", "true").lower() != "false"
    log.info("Bot starting... paper=%s pairs=%s tf=%s",
             paper, config["market"]["pairs"], config["market"]["timeframe"])

    shutdown_event = threading.Event()
    bot = Bot(config, paper=paper, shutdown_event=shutdown_event)

    # Startup tick: reconcile + snapshot so the DB reflects current Alpaca
    # state before the first scheduled signal cycle (otherwise an existing
    # paper position would only show up after the next :00 boundary).
    try:
        bot.executor.reconcile_positions()
    except Exception:
        log.exception("startup reconcile failed (continuing)")
    try:
        bot.equity_snapshot()
    except Exception:
        log.exception("startup equity_snapshot failed (continuing)")

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        bot.signal_cycle,
        CronTrigger(hour="0,4,8,12,16,20", minute=0, second=0, timezone="UTC"),
        id="signal_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        bot.stop_monitor,
        IntervalTrigger(minutes=1),
        id="stop_monitor",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        bot.equity_snapshot,
        CronTrigger(minute=0, second=5, timezone="UTC"),
        id="equity_snapshot",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    # Daily summary fires at 00:00:30 UTC so it runs *after* the 00:00 equity
    # snapshot (second=5 above) — keeps the "equity now" line in the summary
    # fresh on the day it's published.
    scheduler.add_job(
        bot.daily_summary,
        CronTrigger(hour=0, minute=0, second=30, timezone="UTC"),
        id="daily_summary",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    notify("Bot online (paper)" if paper else "Bot online (LIVE)")
    log.info(
        "scheduler started: signal_cycle 4h, stop_monitor 1min, "
        "equity_snapshot 1h, daily_summary 00:00 UTC"
    )

    def _shutdown(*_a: object) -> None:
        shutdown_event.set()
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        # Block until SIGINT/SIGTERM or kill-switch sets the event.
        shutdown_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
