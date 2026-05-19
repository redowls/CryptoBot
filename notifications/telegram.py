"""Telegram notifications — Session 9.

Three things live here:

1. `send_message(text)` — fire-and-forget sync wrapper around python-telegram-
   bot's async `Bot.send_message`. Falls back to log-only when
   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset, so dev work without
   credentials still functions. Never raises; failures land in the local log.

2. `TelegramLogHandler` — `logging.Handler` that mirrors ERROR-and-above
   records to Telegram, deduped to one message per 5-minute window per error
   type. Attached to the root logger by `main.setup_logging` so any module's
   `log.error(...)` reaches the user's phone.

3. `build_daily_summary(repo)` + `send_daily_summary(repo)` — the 00:00 UTC
   end-of-day report: trades closed in the prior 24h, total PnL, open
   positions, current equity vs. ~7-day-ago equity. `build_*` is pure (no
   side effects) so it's testable with a fake repo; `send_*` wraps with the
   Telegram call.

Notes:
- python-telegram-bot v20+ is async-first. Telegram traffic is low (a handful
  of messages per day), so each call wraps the coroutine in `asyncio.run(...)`
  rather than maintaining our own event loop.
- The handler skips records from `notifications.telegram` itself to avoid an
  infinite loop where a Telegram-send failure logs an error that we then try
  to Telegram-notify.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

_RATE_LIMIT_WINDOW = timedelta(minutes=5)
_recent_errors: dict[str, datetime] = {}
_recent_errors_lock = threading.Lock()

_bot_lock = threading.Lock()
_bot_singleton: Any = None


def _get_bot() -> Any:
    """Lazy-build a `telegram.Bot` from env. Returns None if creds missing."""
    global _bot_singleton
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    with _bot_lock:
        if _bot_singleton is None:
            from telegram import Bot  # local import: keep startup fast
            _bot_singleton = Bot(token=token)
        return _bot_singleton


def send_message(text: str) -> bool:
    """Send `text` to TELEGRAM_CHAT_ID. Returns True on success.

    Falls back to log-only when token or chat are unset, so the bot keeps
    running in dev environments without Telegram configured. Never raises:
    notification failures should not cascade into trading-loop failures.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("[telegram-disabled] %s", text)
        return False

    bot = _get_bot()
    if bot is None:
        log.info("[telegram-disabled] %s", text)
        return False

    try:
        chat = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        asyncio.run(bot.send_message(chat_id=chat, text=text))
        return True
    except Exception:
        log.exception("Telegram send failed")
        return False


class TelegramLogHandler(logging.Handler):
    """Mirror ERROR-and-above log records to Telegram, rate-limited.

    Dedupe key is `(logger_name, first 80 chars of message)`. Same exception
    repeated from the same call site collapses to one notification per
    5-minute window — enough to alert without spamming during an outage.
    """
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        # Avoid recursion: a Telegram-send error logs from this module; we'd
        # otherwise try to Telegram-notify that failure and loop.
        if record.name.startswith("notifications.telegram"):
            return

        key = self._dedupe_key(record)
        now = datetime.now(timezone.utc)
        with _recent_errors_lock:
            last = _recent_errors.get(key)
            if last is not None and (now - last) < _RATE_LIMIT_WINDOW:
                return
            _recent_errors[key] = now

        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        # Telegram caps message length at 4096; leave headroom for the prefix.
        body = f"[{record.levelname}] {record.name}\n{msg}"[:3900]
        try:
            send_message(body)
        except Exception:
            # Last-resort: never let a log handler crash the caller.
            pass

    @staticmethod
    def _dedupe_key(record: logging.LogRecord) -> str:
        try:
            head = (record.getMessage() or "")[:80]
        except Exception:
            head = record.msg if isinstance(record.msg, str) else ""
        raw = f"{record.name}|{head}"
        return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


# ---------- daily summary ----------

def build_daily_summary(repo: Any, now: datetime | None = None) -> str:
    """Compose the 00:00 UTC end-of-day report.

    "Today's trades" at exactly 00:00 UTC would be empty by definition, so the
    summary reports the day that just closed: `[now-24h, now)`. Equity change
    compares the latest snapshot against the most recent snapshot taken on or
    before `now - 7 days`.
    """
    now = now or datetime.now(timezone.utc)
    day_end = now.replace(minute=0, second=0, microsecond=0)
    day_start = day_end - timedelta(days=1)
    week_ref = day_end - timedelta(days=7)

    # Trades closed in the prior 24h.
    trades_df = repo.db.query_df(
        "SELECT symbol, qty, entry_price, exit_price, pnl_usd, pnl_pct, "
        "entry_ts, exit_ts FROM trades "
        "WHERE status = 'CLOSED' AND exit_ts >= ? AND exit_ts < ? "
        "ORDER BY exit_ts ASC;",
        (_naive_utc(day_start), _naive_utc(day_end)),
    )

    total_pnl = 0.0
    trade_lines: list[str] = []
    for _, r in trades_df.iterrows():
        pnl = float(r["pnl_usd"]) if r["pnl_usd"] is not None else 0.0
        pct = float(r["pnl_pct"]) if r["pnl_pct"] is not None else 0.0
        total_pnl += pnl
        trade_lines.append(f"  {r['symbol']}: {pnl:+.2f} USD ({pct * 100:+.2f}%)")

    # Open positions.
    positions = repo.get_open_positions_with_stops()
    pos_lines: list[str] = []
    for p in positions:
        stop = p.get("current_stop")
        stop_txt = f" stop={stop:.2f}" if stop is not None else ""
        pos_lines.append(
            f"  {p['symbol']}: qty={p['qty']:.6f} "
            f"avg_entry={p['avg_entry_price']:.2f}{stop_txt}"
        )

    # Equity now vs. ~7 days ago.
    latest_df = repo.db.query_df(
        "SELECT TOP 1 equity FROM account_snapshots ORDER BY ts DESC;"
    )
    week_df = repo.db.query_df(
        "SELECT TOP 1 equity FROM account_snapshots WHERE ts <= ? "
        "ORDER BY ts DESC;",
        (_naive_utc(week_ref),),
    )
    latest_equity = float(latest_df.iloc[0]["equity"]) if not latest_df.empty else None
    week_equity = float(week_df.iloc[0]["equity"]) if not week_df.empty else None

    parts: list[str] = [f"Daily summary {day_start.date()} (UTC)"]

    if trade_lines:
        parts.append(f"\nClosed trades ({len(trade_lines)}), PnL {total_pnl:+.2f} USD:")
        parts.extend(trade_lines)
    else:
        parts.append("\nClosed trades: none")

    if pos_lines:
        parts.append(f"\nOpen positions ({len(pos_lines)}):")
        parts.extend(pos_lines)
    else:
        parts.append("\nOpen positions: none")

    if latest_equity is not None:
        parts.append(f"\nEquity: {latest_equity:,.2f} USD")
        if week_equity is not None and week_equity > 0:
            delta = latest_equity - week_equity
            pct = delta / week_equity * 100
            parts.append(f"Week change: {delta:+,.2f} USD ({pct:+.2f}%)")
    else:
        parts.append("\nEquity: (no snapshot)")

    return "\n".join(parts)


def send_daily_summary(repo: Any) -> bool:
    """Build the summary and send it. Used as an APScheduler job target."""
    try:
        text = build_daily_summary(repo)
    except Exception:
        log.exception("Failed to build daily summary")
        return False
    return send_message(text)


# ---------- helpers ----------

def _naive_utc(ts: datetime) -> datetime:
    """Strip tzinfo to match the DATETIME2 (naive UTC) columns in SQL."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _reset_rate_limit_for_tests() -> None:
    """Test hook: clear the dedupe map so each test starts fresh."""
    with _recent_errors_lock:
        _recent_errors.clear()
