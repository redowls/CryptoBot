"""Evening end-of-day report — runs daily at 15:00 UTC (22:00 Asia/Bangkok).

Complements the built-in 00:00 UTC daily summary in notifications.telegram by
answering the question: "what did the bot do (or NOT do) in the last 24h, and
why?". Reports today's signals, trade activity, open positions, recent errors,
and equity change.

Run:
    cd /home/trader/cryptobot
    venv/bin/python -m scripts.evening_report                # send to Telegram
    venv/bin/python -m scripts.evening_report --print-only   # print, do not send

Designed to be invoked from cron as the `trader` user with cwd=APP_DIR.

Exit codes:
    0 — report built and sent (or printed)
    1 — DB or Telegram failure
    2 — argparse / env error
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from db.connection import Database
from notifications.telegram import send_message

PREFIX = "[Claude Routine] Evening Report"


def _naive_utc(dt: datetime) -> datetime:
    """SQL Server DATETIME2 is tz-naive UTC — strip tzinfo for parameter binding."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _tag_utc(dt: datetime) -> datetime:
    """Tag a DB-returned naive datetime as UTC so it can be compared."""
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def build_report(db: Database, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=1)

    parts: list[str] = [
        PREFIX,
        f"Window: last 24h ending {now.strftime('%Y-%m-%d %H:%M UTC')}",
    ]

    # --- Signals in the last 24h -------------------------------------------
    sig_df = db.query_df(
        "SELECT id, ts, symbol, side, signal_price, proposed_qty, proposed_stop, notes "
        "FROM signals WHERE ts >= ? ORDER BY ts ASC;",
        (_naive_utc(window_start),),
    )

    if sig_df.empty:
        # Find the most recent signal to give context — Donchian can be quiet
        # for days/weeks legitimately.
        last_df = db.query_df("SELECT TOP 1 ts, symbol, side FROM signals ORDER BY ts DESC;")
        if last_df.empty:
            parts.append("\nSignals (24h): none — no signals on record yet")
        else:
            last_ts = _tag_utc(last_df.iloc[0]["ts"])
            age = now - last_ts
            parts.append(
                f"\nSignals (24h): none — last signal {age.days}d "
                f"{age.seconds // 3600}h ago "
                f"({last_df.iloc[0]['side']} {last_df.iloc[0]['symbol']})"
            )
            parts.append(
                "  Donchian only fires on a fresh 20-bar breakout — quiet "
                "ranges produce no signals (normal)."
            )
    else:
        parts.append(f"\nSignals (24h): {len(sig_df)}")
        for _, r in sig_df.iterrows():
            ts = _tag_utc(r["ts"]).strftime("%H:%M")
            line = f"  {ts} {r['side']:>4} {r['symbol']} @ {float(r['signal_price']):.2f}"
            if r["proposed_qty"] is not None:
                line += f"  qty={float(r['proposed_qty']):.6f}"
            if r["proposed_stop"] is not None:
                line += f"  stop={float(r['proposed_stop']):.2f}"
            if r["notes"]:
                line += f"  ({str(r['notes'])[:80]})"
            parts.append(line)

    # --- Entry follow-through (did each signal turn into an order?) --------
    if not sig_df.empty:
        signal_ids = [int(x) for x in sig_df["id"].tolist()]
        placeholders = ",".join("?" for _ in signal_ids)
        linked_df = db.query_df(
            f"SELECT DISTINCT signal_id FROM orders WHERE signal_id IN ({placeholders});",
            tuple(signal_ids),
        )
        linked_count = len(linked_df)
        suppressed = len(signal_ids) - linked_count
        if suppressed > 0:
            parts.append(
                f"\nEntry NOT taken on {suppressed} of {len(signal_ids)} signals "
                "— check ERROR/WARN logs below or `journalctl -u cryptobot` "
                "for the gate that blocked entry (kill switch, position already "
                "open, sizing failure, etc.)."
            )

    # --- Trades activity (entries and exits in window) ---------------------
    entered_df = db.query_df(
        "SELECT symbol, entry_price, qty, entry_ts FROM trades "
        "WHERE entry_ts >= ? ORDER BY entry_ts ASC;",
        (_naive_utc(window_start),),
    )
    closed_df = db.query_df(
        "SELECT symbol, entry_price, exit_price, qty, pnl_usd, pnl_pct, "
        "entry_ts, exit_ts FROM trades "
        "WHERE status = 'CLOSED' AND exit_ts >= ? ORDER BY exit_ts ASC;",
        (_naive_utc(window_start),),
    )

    if entered_df.empty and closed_df.empty:
        parts.append("\nTrades (24h): no entries, no exits")
    else:
        if not entered_df.empty:
            parts.append(f"\nEntries (24h): {len(entered_df)}")
            for _, r in entered_df.iterrows():
                ts = _tag_utc(r["entry_ts"]).strftime("%H:%M")
                parts.append(
                    f"  {ts} {r['symbol']} qty={float(r['qty']):.6f} @ "
                    f"{float(r['entry_price']):.2f}"
                )
        if not closed_df.empty:
            total = sum(float(r["pnl_usd"] or 0) for _, r in closed_df.iterrows())
            parts.append(f"\nExits (24h): {len(closed_df)}, total PnL {total:+.2f} USD")
            for _, r in closed_df.iterrows():
                ts = _tag_utc(r["exit_ts"]).strftime("%H:%M")
                pnl = float(r["pnl_usd"] or 0)
                pct = float(r["pnl_pct"] or 0) * 100
                parts.append(
                    f"  {ts} {r['symbol']} {pnl:+.2f} USD ({pct:+.2f}%)"
                )

    # --- Open positions (current snapshot) ---------------------------------
    pos_df = db.query_df(
        "SELECT symbol, qty, avg_entry_price, current_stop FROM positions "
        "WHERE qty <> 0 ORDER BY symbol;"
    )
    if pos_df.empty:
        parts.append("\nOpen positions: none")
    else:
        parts.append(f"\nOpen positions ({len(pos_df)}):")
        for _, r in pos_df.iterrows():
            stop = r["current_stop"]
            stop_txt = f" stop={float(stop):.2f}" if stop is not None else ""
            parts.append(
                f"  {r['symbol']} qty={float(r['qty']):.6f} "
                f"avg={float(r['avg_entry_price']):.2f}{stop_txt}"
            )

    # --- Errors / warnings in the window -----------------------------------
    err_df = db.query_df(
        "SELECT level, COUNT(*) AS n FROM logs "
        "WHERE ts >= ? AND level IN ('ERROR','WARN','WARNING') "
        "GROUP BY level;",
        (_naive_utc(window_start),),
    )
    if err_df.empty:
        parts.append("\nErrors (24h): none")
    else:
        breakdown = ", ".join(f"{r['level']}={int(r['n'])}" for _, r in err_df.iterrows())
        parts.append(f"\nErrors (24h): {breakdown}")
        recent = db.query_df(
            "SELECT TOP 3 ts, level, module, LEFT(message, 120) AS msg FROM logs "
            "WHERE ts >= ? AND level IN ('ERROR','WARN','WARNING') "
            "ORDER BY ts DESC;",
            (_naive_utc(window_start),),
        )
        for _, r in recent.iterrows():
            ts = _tag_utc(r["ts"]).strftime("%H:%M")
            mod = (r["module"] or "?")[:20]
            parts.append(f"  {ts} {r['level']} {mod}: {r['msg']}")

    # --- Equity ------------------------------------------------------------
    latest_df = db.query_df("SELECT TOP 1 ts, equity FROM account_snapshots ORDER BY ts DESC;")
    day_df = db.query_df(
        "SELECT TOP 1 equity FROM account_snapshots WHERE ts <= ? ORDER BY ts DESC;",
        (_naive_utc(window_start),),
    )
    if latest_df.empty:
        parts.append("\nEquity: (no snapshot)")
    else:
        latest_eq = float(latest_df.iloc[0]["equity"])
        snap_ts = _tag_utc(latest_df.iloc[0]["ts"]).strftime("%H:%M")
        line = f"\nEquity (as of {snap_ts}): {latest_eq:,.2f} USD"
        if not day_df.empty:
            day_eq = float(day_df.iloc[0]["equity"])
            if day_eq > 0:
                delta = latest_eq - day_eq
                pct = delta / day_eq * 100
                line += f"  (24h {delta:+,.2f} / {pct:+.2f}%)"
        parts.append(line)

    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="cryptobot evening report")
    ap.add_argument(
        "--print-only",
        action="store_true",
        help="print to stdout instead of sending to Telegram (for manual testing)",
    )
    args = ap.parse_args()

    load_dotenv()

    try:
        db = Database()
        text = build_report(db)
    except Exception as e:
        err = f"{PREFIX}\nFAILED to build report: {type(e).__name__}: {e}"
        if args.print_only:
            print(err)
        else:
            send_message(err)
        return 1

    if args.print_only:
        print(text)
        return 0

    ok = send_message(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
