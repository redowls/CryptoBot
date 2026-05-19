"""Notification module tests + script-mode live Telegram send.

Pytest mode is fully offline — exercises the rate limiter, the env-var
fallback, and the daily-summary builder against a fake repo. Run with::

    python -m pytest tests/test_notifications.py

Script mode sends a real Telegram message using the credentials in .env::

    python -m tests.test_notifications              # send hello + daily summary
    python -m tests.test_notifications --summary    # send daily summary only

Script mode prints status to stdout and exits non-zero if either send fails
(missing token or chat is treated as a setup error and surfaced clearly).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from dotenv import load_dotenv

import notifications.telegram as tg

load_dotenv()


@pytest.fixture(autouse=True)
def _clear_rate_limit() -> None:
    """Each test starts with an empty dedupe map so order doesn't matter."""
    tg._reset_rate_limit_for_tests()


# ---------- send_message env-var fallback ----------

def test_send_message_returns_false_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert tg.send_message("hello") is False


def test_send_message_returns_false_without_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert tg.send_message("hello") is False


def test_send_message_swallows_send_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Telegram API failure must not propagate to the caller — trading loop
    survives notification outages."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    fake_bot = MagicMock()
    fake_bot.send_message.side_effect = RuntimeError("network down")
    with patch.object(tg, "_get_bot", return_value=fake_bot):
        assert tg.send_message("hello") is False


# ---------- TelegramLogHandler rate limiting ----------

def test_handler_ignores_below_error() -> None:
    handler = tg.TelegramLogHandler()
    record = logging.LogRecord(
        name="x", level=logging.WARNING, pathname="", lineno=0,
        msg="warn", args=(), exc_info=None,
    )
    with patch.object(tg, "send_message") as send:
        handler.emit(record)
        send.assert_not_called()


def test_handler_dedupes_same_error_within_window() -> None:
    handler = tg.TelegramLogHandler()
    rec = logging.LogRecord(
        name="risk.manager", level=logging.ERROR, pathname="", lineno=0,
        msg="DB connection lost", args=(), exc_info=None,
    )
    with patch.object(tg, "send_message") as send:
        handler.emit(rec)
        handler.emit(rec)
        handler.emit(rec)
        assert send.call_count == 1


def test_handler_distinct_errors_send_separately() -> None:
    handler = tg.TelegramLogHandler()
    rec_a = logging.LogRecord(
        name="risk.manager", level=logging.ERROR, pathname="", lineno=0,
        msg="DB connection lost", args=(), exc_info=None,
    )
    rec_b = logging.LogRecord(
        name="execution.alpaca_executor", level=logging.ERROR, pathname="",
        lineno=0, msg="submit failed", args=(), exc_info=None,
    )
    with patch.object(tg, "send_message") as send:
        handler.emit(rec_a)
        handler.emit(rec_b)
        assert send.call_count == 2


def test_handler_resends_after_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forge the dedupe map to look like an old entry; the next emit should
    bypass the rate limit and fire again."""
    handler = tg.TelegramLogHandler()
    rec = logging.LogRecord(
        name="risk.manager", level=logging.ERROR, pathname="", lineno=0,
        msg="DB connection lost", args=(), exc_info=None,
    )
    with patch.object(tg, "send_message") as send:
        handler.emit(rec)
        # Rewind the recorded timestamp by 10 minutes so it's outside the 5-min
        # window. Direct dict access is fine — this is the test hook the module
        # exposes.
        key = next(iter(tg._recent_errors))
        tg._recent_errors[key] = datetime.now(timezone.utc) - timedelta(minutes=10)
        handler.emit(rec)
        assert send.call_count == 2


def test_handler_skips_self_to_avoid_recursion() -> None:
    """A failed Telegram send logs from notifications.telegram itself. If the
    handler picked that up we'd recurse — verify it's filtered."""
    handler = tg.TelegramLogHandler()
    rec = logging.LogRecord(
        name="notifications.telegram", level=logging.ERROR, pathname="",
        lineno=0, msg="Telegram send failed", args=(), exc_info=None,
    )
    with patch.object(tg, "send_message") as send:
        handler.emit(rec)
        send.assert_not_called()


# ---------- daily summary ----------

@dataclass
class _FakeDB:
    trades: pd.DataFrame
    latest_equity: pd.DataFrame
    week_equity: pd.DataFrame

    def query_df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        s = sql.strip().upper()
        if "FROM TRADES" in s:
            return self.trades
        if "FROM ACCOUNT_SNAPSHOTS" in s:
            # Distinguish the two snapshot queries by their WHERE clause.
            return self.week_equity if "WHERE TS" in s else self.latest_equity
        raise AssertionError(f"unexpected query: {sql!r}")


class _FakeRepo:
    def __init__(self, trades: pd.DataFrame, positions: list[dict],
                 latest_equity: float | None, week_equity: float | None) -> None:
        latest_df = (
            pd.DataFrame({"equity": [latest_equity]})
            if latest_equity is not None else pd.DataFrame({"equity": []})
        )
        week_df = (
            pd.DataFrame({"equity": [week_equity]})
            if week_equity is not None else pd.DataFrame({"equity": []})
        )
        self.db = _FakeDB(trades=trades, latest_equity=latest_df, week_equity=week_df)
        self._positions = positions

    def get_open_positions_with_stops(self) -> list[dict]:
        return self._positions


def test_daily_summary_no_trades_no_positions() -> None:
    repo = _FakeRepo(
        trades=pd.DataFrame(columns=[
            "symbol", "qty", "entry_price", "exit_price",
            "pnl_usd", "pnl_pct", "entry_ts", "exit_ts",
        ]),
        positions=[],
        latest_equity=10_000.0,
        week_equity=9_500.0,
    )
    text = tg.build_daily_summary(repo, now=datetime(2026, 5, 19, tzinfo=timezone.utc))
    assert "Closed trades: none" in text
    assert "Open positions: none" in text
    assert "10,000.00" in text
    assert "+500.00" in text  # week change line
    assert "+5.26%" in text


def test_daily_summary_with_trades_and_positions() -> None:
    repo = _FakeRepo(
        trades=pd.DataFrame([
            {"symbol": "BTC/USD", "qty": 0.01, "entry_price": 60000,
             "exit_price": 62000, "pnl_usd": 20.0, "pnl_pct": 0.0333,
             "entry_ts": datetime(2026, 5, 18, 12), "exit_ts": datetime(2026, 5, 18, 18)},
            {"symbol": "ETH/USD", "qty": 0.1, "entry_price": 3000,
             "exit_price": 2950, "pnl_usd": -5.0, "pnl_pct": -0.0167,
             "entry_ts": datetime(2026, 5, 18, 8), "exit_ts": datetime(2026, 5, 18, 22)},
        ]),
        positions=[
            {"symbol": "BTC/USD", "qty": 0.02, "avg_entry_price": 61000.0,
             "current_stop": 58000.0, "opened_at": None},
        ],
        latest_equity=10_100.0,
        week_equity=10_000.0,
    )
    text = tg.build_daily_summary(repo, now=datetime(2026, 5, 19, tzinfo=timezone.utc))
    assert "Closed trades (2)" in text
    assert "+15.00" in text  # net PnL: +20 + -5
    assert "BTC/USD" in text and "+20.00" in text
    assert "ETH/USD" in text and "-5.00" in text
    assert "Open positions (1)" in text
    assert "stop=58000.00" in text


def test_daily_summary_handles_missing_equity() -> None:
    repo = _FakeRepo(
        trades=pd.DataFrame(columns=[
            "symbol", "qty", "entry_price", "exit_price",
            "pnl_usd", "pnl_pct", "entry_ts", "exit_ts",
        ]),
        positions=[],
        latest_equity=None,
        week_equity=None,
    )
    text = tg.build_daily_summary(repo, now=datetime(2026, 5, 19, tzinfo=timezone.utc))
    assert "(no snapshot)" in text


# ---------- script mode ----------

def _script_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true",
                        help="send a real daily summary built from the live DB")
    parser.add_argument(
        "--text",
        default="cryptobot session 9 smoke test",
        help="message body for the hello send",
    )
    args = parser.parse_args()

    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        print("       create the bot via @BotFather and look up your chat id")
        print("       (e.g. via https://api.telegram.org/bot<token>/getUpdates)")
        return 2

    ok = tg.send_message(args.text)
    print(f"hello send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1

    if args.summary:
        from db.repository import Repository
        repo = Repository()
        text = tg.build_daily_summary(repo)
        print("--- summary preview ---")
        print(text)
        print("-----------------------")
        time.sleep(1)  # avoid hitting Telegram's per-second rate limit
        ok2 = tg.send_message(text)
        print(f"summary send: {'ok' if ok2 else 'FAILED'}")
        if not ok2:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_script_main())
