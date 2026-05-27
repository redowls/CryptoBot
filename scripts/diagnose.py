"""One-shot diagnostic — run from APP_DIR via `venv/bin/python -m scripts.diagnose`."""
from dotenv import load_dotenv
load_dotenv()
from db.connection import Database

db = Database()

print("=== signals (total + last 5) ===")
print(db.query_df("SELECT COUNT(*) AS n FROM signals;").to_string(index=False))
print()
print(db.query_df("SELECT TOP 5 ts, symbol, side, signal_price, notes FROM signals ORDER BY ts DESC;").to_string(index=False))

print("\n=== market_data freshness per symbol/timeframe ===")
print(db.query_df("""
    SELECT symbol, timeframe, COUNT(*) AS bars, MIN(ts) AS first_ts, MAX(ts) AS last_ts
    FROM market_data
    GROUP BY symbol, timeframe
    ORDER BY symbol, timeframe;
""").to_string(index=False))

print("\n=== orders (total + last 5) ===")
print(db.query_df("SELECT COUNT(*) AS n FROM orders;").to_string(index=False))
print()
print(db.query_df("SELECT TOP 5 submitted_at, symbol, side, status FROM orders ORDER BY submitted_at DESC;").to_string(index=False))

print("\n=== account_snapshots (total + first/last) ===")
print(db.query_df("SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM account_snapshots;").to_string(index=False))

print("\n=== recent ERROR/WARN logs (last 24h) ===")
print(db.query_df("""
    SELECT TOP 20 ts, level, module, LEFT(message, 150) AS msg
    FROM logs
    WHERE level IN ('ERROR','WARN','WARNING')
      AND ts >= DATEADD(HOUR, -24, SYSUTCDATETIME())
    ORDER BY ts DESC;
""").to_string(index=False))

print("\n=== Donchian channel proximity (per symbol on 4Hour) ===")
print("If close is near the high-20 or low-20 channel boundary, a breakout is imminent.")
print(db.query_df("""
    WITH latest AS (
        SELECT symbol, MAX(ts) AS max_ts FROM market_data
        WHERE timeframe = '4Hour' GROUP BY symbol
    ),
    last_21 AS (
        SELECT md.symbol, md.ts, md.high_px, md.low_px, md.close_px,
               ROW_NUMBER() OVER (PARTITION BY md.symbol ORDER BY md.ts DESC) AS rn
        FROM market_data md
        WHERE md.timeframe = '4Hour'
    )
    SELECT
        symbol,
        MAX(CASE WHEN rn = 1 THEN close_px END) AS close_now,
        MAX(CASE WHEN rn BETWEEN 2 AND 21 THEN high_px END) AS donchian_high_20,
        MIN(CASE WHEN rn BETWEEN 2 AND 21 THEN low_px END) AS donchian_low_20,
        MAX(CASE WHEN rn = 1 THEN ts END) AS last_bar_ts
    FROM last_21
    WHERE rn <= 21
    GROUP BY symbol
    ORDER BY symbol;
""").to_string(index=False))
