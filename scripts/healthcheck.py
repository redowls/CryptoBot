"""External health probe for cryptobot. Exits non-zero if any check fails.

Designed to be run from cron, an uptime monitor, or by hand:
    /home/trader/cryptobot/venv/bin/python -m scripts.healthcheck
    /home/trader/cryptobot/venv/bin/python -m scripts.healthcheck --max-signal-age-hours 12

Checks (in order):
    1. SQL Server reachable + `cryptobot` DB query succeeds
    2. Alpaca trading API reachable (account fetch returns a non-empty account_number)
    3. Most recent row in `signals` is newer than --max-signal-age-hours

This is intentionally process-external: it does NOT import main.py / Bot, so a
crashed scheduler thread can't fool the probe into reporting healthy. Each
check prints PASS/FAIL on its own line; final summary line is `OK` or `FAIL`.

Exit codes:
    0 — all checks passed
    1 — at least one check failed
    2 — argparse / env error
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# Repo-root imports — `python -m scripts.healthcheck` from APP_DIR works,
# and the systemd-installed venv's site-packages includes the cryptobot tree
# because we run from WorkingDirectory.
from data.alpaca_data import AlpacaDataClient
from db.connection import Database


def _check_db() -> tuple[bool, str]:
    try:
        db = Database()
        df = db.query_df("SELECT 1 AS ok;")
        if df.empty or int(df.iloc[0]["ok"]) != 1:
            return False, "SELECT 1 returned unexpected result"
        return True, "SQL Server reachable"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_alpaca() -> tuple[bool, str]:
    try:
        client = AlpacaDataClient()
        acct = client.get_account_info()
        # account_number is a stable, non-empty string on any real account
        if not getattr(acct, "account_number", None):
            return False, "account.account_number empty"
        return True, f"Alpaca OK (account={acct.account_number}, equity={acct.equity})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_last_signal(max_age_hours: float) -> tuple[bool, str]:
    """Stale signals usually mean the scheduler is wedged. A *deliberately*
    quiet market is fine — Donchian can go weeks without a signal — so
    --max-signal-age-hours is tunable. Default 168h (one week) matches the
    longest no-signal stretches seen historically on BTC/ETH 4h."""
    try:
        db = Database()
        df = db.query_df("SELECT TOP 1 ts FROM signals ORDER BY ts DESC;")
        if df.empty:
            return False, "signals table is empty"
        last = df.iloc[0]["ts"]
        # SQL Server DATETIME2 comes back tz-naive UTC; tag it before comparing.
        if hasattr(last, "tzinfo") and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last
        if age > timedelta(hours=max_age_hours):
            return False, f"last signal {age} ago (> {max_age_hours}h cap)"
        return True, f"last signal {age} ago"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="cryptobot health probe")
    ap.add_argument(
        "--max-signal-age-hours",
        type=float,
        default=168.0,
        help="fail if newest signals.ts is older than this (default 168h = 1 week)",
    )
    ap.add_argument(
        "--skip-signal-age",
        action="store_true",
        help="skip the last-signal-age check (useful on first deploy before any signals exist)",
    )
    args = ap.parse_args()

    load_dotenv()

    checks: list[tuple[str, bool, str]] = []
    ok, msg = _check_db()
    checks.append(("db", ok, msg))
    ok, msg = _check_alpaca()
    checks.append(("alpaca", ok, msg))
    if not args.skip_signal_age:
        ok, msg = _check_last_signal(args.max_signal_age_hours)
        checks.append(("signals", ok, msg))

    all_ok = all(c[1] for c in checks)
    for name, ok, msg in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}")
    print("OK" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
