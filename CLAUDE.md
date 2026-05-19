# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Sessions 1–9 of 10 complete (data client, DB schema/repo, historical ingest, Donchian strategy, vectorbt backtest, Alpaca executor + APScheduler wiring in main.py, RiskManager + 1-min stop monitor + 1-hr equity snapshot job, real Telegram bridge + rate-limited ERROR mirror + 00:00 UTC daily summary + top-level crash handler). Session 10 (systemd deploy) is unstarted. [initial.md](initial.md) is the 450-line build plan and source of truth for design decisions — read it before implementation work; each session's verbatim prompt and acceptance test lives in §6. This working tree is not under git yet, so there is no commit history to defer to — per-session deliverables live in §6 of initial.md.

## Project purpose

Build a Python crypto trading bot that runs 24/7 on a Linux VPS, trades BTC/USD and ETH/USD via Alpaca (paper first, live later), persists state to SQL Server 2025 Express on the same VPS, and notifies via Telegram. The user is based in Indonesia; Alpaca crypto is available there but live KYC is a separate gate — Binance is the documented fallback.

## Dev environment

Development happens on Windows (this working tree); deployment target is Ubuntu 24.04 under systemd. Path-sensitive code (file paths, line endings, ODBC driver names) must work on both — prefer `pathlib`, avoid hardcoded `\` separators.

Common commands (PowerShell):

```powershell
venv\Scripts\Activate.ps1                     # activate venv
pip install -r requirements.txt               # install deps
python main.py                                # runs the bot: reconcile + APScheduler 4h cron
python -m data.ingest --symbol BTC/USD --timeframe 4Hour --days 365   # populate market_data
python -m pytest tests/                       # run all tests
python -m pytest tests/test_foo.py::test_bar  # run one test
```

On Linux/VPS, use `source venv/bin/activate` instead. Use `pytest` (not unittest) per [initial.md](initial.md) §6.

Test files are designed to be runnable two ways (see [tests/test_data.py](tests/test_data.py) for the pattern): `python -m pytest tests/test_data.py` runs assertions only; `python -m tests.test_data` runs the same module as a script and prints diagnostic output (bar counts, latest quote, account state). Preserve this dual-mode shape for new smoke tests — it's how the user manually verifies live API integration without spinning up pytest.

First-run setup the tests assume: copy `.env.example` → `.env` and fill in `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` plus `DB_*` vars. `tests/test_ingest.py` and `tests/test_strategy.py` (script mode) read bars from SQL Server, so the DB must be populated via `python -m data.ingest` before they're useful; strategy script mode falls back to a live Alpaca fetch if the DB is empty. **`tests/test_executor.py` script mode submits a real ~$10 paper market BUY** — don't run it unless you mean to take a position. Pytest mode of the same file is fully offline (mocked TradingClient).

Script-mode smoke tests run under the Windows `cp1252` console, which can't encode non-ASCII characters (e.g. `→`, `≥`). Stick to ASCII in `print()`s in the `if __name__ == "__main__"` block; reserve unicode for pytest assertions (UTF-8) and docstrings.

## Stack (locked decisions from the plan)

- **Python 3.11+**, `alpaca-py==0.43.4` (not the deprecated `alpaca-trade-api`)
- **SQL Server 2025 Express on Ubuntu 24.04** (requires CU1+), accessed via `pyodbc` + **ODBC Driver 18**
- **APScheduler** `BackgroundScheduler` (in-process, not OS cron) — 4h cron for signals, 1-min for stop monitoring, 1h for equity snapshots
- **vectorbt** for backtesting (free edition)
- **systemd** for 24/7 operation (not Docker) — unit file template in `initial.md` §5
- **python-telegram-bot** for notifications
- Config: `.env` (secrets) + `config/config.yaml` (strategy params)

## Module layout

Implemented: [data/](data/) (`AlpacaDataClient`), [db/](db/) (`Database` + `Repository` + schema.sql), [strategy/](strategy/) (`DonchianStrategy`), [backtest/](backtest/) (`run_backtest` via vectorbt), [execution/](execution/) (`Executor` — Alpaca order submit + position reconcile), [risk/sizing.py](risk/sizing.py) (`compute_position_qty`) + [risk/manager.py](risk/manager.py) (`RiskManager` — composes sizing, owns the entry gates, kill switch, and stop monitor), [notifications/telegram.py](notifications/telegram.py) (`send_message` sync wrapper around python-telegram-bot's async `Bot.send_message`, `TelegramLogHandler` for ERROR-level mirror, `build_daily_summary`/`send_daily_summary` for the 00:00 UTC report), [config/](config/) (yaml), [tests/](tests/), [main.py](main.py) (APScheduler `BackgroundScheduler` running `signal_cycle` 4h, `stop_monitor` 1min, `equity_snapshot` 1h, `daily_summary` 00:00:30 UTC; top-level try/except in `main()` logs CRITICAL + Telegrams + re-raises so systemd restarts).

Not yet built: `deploy/` (Session 10 — systemd unit + install/update scripts). `logs/` exists but is gitignored (rotating file logs land here at runtime). `backtest/results/` is created on first plot save and holds the per-symbol equity-curve PNGs.

## Architectural invariants

These are easy to get wrong and have explicit rationale in the plan — preserve them:

- **Look-ahead bias**: Donchian channels MUST be shifted by 1 bar (`rolling().shift(1)`) before signal evaluation. This is the most common quant bug. For historical replay/backtest, use [strategy/donchian.py](strategy/donchian.py) `replay_signals` — it walks bars in order and preserves the shift; don't reimplement the loop in backtest code.
- **Strategy is long-only**: Alpaca crypto does not support shorting. Don't add short logic.
- **Entry filter is two-timeframe**: 4h close > prior 20-bar Donchian high AND daily close > daily 200-EMA. Both required.
- **Position sizing is volatility-adjusted**: `size = (equity * 0.01) / (2 * ATR)`, capped at 25% equity per pair. Never hardcode a notional size.
- **Risk caps are hard stops**: max 2 open positions, 3% daily loss halts new entries for 24h, 15% peak-to-trough drawdown trips a kill switch.
- **Order idempotency**: every order gets a `client_order_id` of the form `{SYMBOL}-{UTC_TIMESTAMP}-{uuid6}` and is persisted to `orders` before submission. Never submit without one.
- **State reconciliation**: on every cycle, reconcile SQL `positions` table against Alpaca `/v2/positions` — Alpaca is authoritative.
- **Paper vs live**: the only difference is `TradingClient(paper=True/False)` and the API key pair. Don't fork code paths.

## Database

Authoritative schema is [db/schema.sql](db/schema.sql) (7 tables: `market_data`, `signals`, `orders`, `trades`, `positions`, `account_snapshots`, `logs`); rationale is in [initial.md](initial.md) §4. Apply with `sqlcmd -S <server> -i db/schema.sql` — the file is **not idempotent** for tables (only `CREATE DATABASE` is guarded), so drop tables manually to re-apply.

Conventions enforced by [db/repository.py](db/repository.py):
- All DB writes go through `Repository`, never raw SQL in business modules. It owns parameter coercion (`_dec`, `_to_utc_naive`) so callers can pass floats, `Decimal`, `pd.Timestamp`, or `datetime` interchangeably.
- All inserts use `?` placeholders; never f-string SQL.
- Upserts use SQL Server `MERGE` (see `insert_bars`, `upsert_position`) — `market_data` has `UNIQUE (symbol, timeframe, ts)` so re-ingestion must be idempotent.
- Timestamps go in as **naive UTC** (`_to_utc_naive` strips tzinfo) because `DATETIME2` columns are timezone-unaware. Bar `ts` is bar-start.
- **Decimal scale matters**: `_dec`/`_dec_or_none` take a `scale` arg and `insert_bars` passes `scale=8` to match the `DECIMAL(*, 8)` price columns. Raw `Decimal(str(float))` produces trailing-precision noise that overflows column scale — always pass the right scale when adding new numeric inserts.
- Connection uses ODBC Driver 18 with `TrustServerCertificate=yes`; credentials come from `DB_SERVER`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` env vars via [.env](.env.example). The `Database.get_connection()` context manager commits on clean exit and rolls back on exception — don't manage transactions manually inside it.

## Execution

[execution/alpaca_executor.py](execution/alpaca_executor.py) `Executor` is the only thing in the codebase allowed to touch Alpaca's trading API. Conventions worth preserving:

- **Pre-submit insert.** `submit_market_order` writes an `orders` row with `status='pending'` **before** calling Alpaca, then updates that row with `alpaca_order_id` + fill data on response (or `status='error'` on raise). If the call crashes mid-flight, the orphaned `pending` row plus the `client_order_id` lets you recover via `TradingClient.get_order_by_client_id(coid)` → patch SQL — rather than blindly resubmitting.
- **alpaca-py returns Enums, not strings.** `OrderStatus.NEW`, `OrderSide.BUY`, etc. are `Enum` objects; `str(enum)` produces `"OrderStatus.NEW"` which overflows the `VARCHAR(20)` `orders.status` column. `_model_dump()` calls `model_dump(mode="json")` to coerce enums to their `.value` and datetimes to ISO strings; `_enum_value()` is the belt-and-braces fallback. Any new code that reads alpaca-py model fields and writes them to SQL must go through one of these.
- **Symbol normalization.** Alpaca crypto historically returned `BTCUSD`; modern alpaca-py returns `BTC/USD`. `_normalize_crypto_symbol` handles both so the SQL `positions.symbol` key stays consistent — use it whenever you read a symbol off an Alpaca response.
- **Reconcile preserves stops.** `reconcile_positions` writes `current_stop=None` on every upsert; the MERGE in `Repository.upsert_position` does `COALESCE(src.current_stop, tgt.current_stop)` so the risk module's stop survives every reconcile. Don't "fix" that None — it's load-bearing.

The bot's runtime loop ([main.py](main.py)) calls `reconcile_positions` at startup and at the top of every 4h `signal_cycle`. Session 8 added a 1-minute `stop_monitor` job and a 1-hour `equity_snapshot` job on the same `BackgroundScheduler`.

## Risk

[risk/manager.py](risk/manager.py) `RiskManager` is the only place enforcing entry gates and the kill switch:

- **Drawdown beats daily loss.** `gate_new_entry` checks drawdown first; if both gates would trip on the same equity reading, the manager reports `KILL_SWITCH` (irreversible) rather than `HALT_DAILY_LOSS` (soft). Don't reorder the checks.
- **Daily anchor falls back across midnight.** `Repository.get_daily_anchor_equity` returns the first snapshot today, falling back to the most recent prior snapshot. A fresh bot start has no anchor only if `account_snapshots` is empty — that's why `main()` runs `equity_snapshot()` at startup before the scheduler boots.
- **Kill switch ends the process via the shutdown Event, not `sys.exit`.** `Bot.__init__` wires `on_kill_switch=shutdown_event.set` so the kill switch (which fires in a scheduler thread) unblocks the main thread's `event.wait()` and exits cleanly under systemd. `trigger_kill_switch` is idempotent — a second call is a no-op.
- **Stops compare against bid, not mid.** A long position would clear into the bid on a market sell, so bid is the realistic exit price. `_quote_price` falls back to ask only if bid is missing.
- **Stop monitor deletes positions, doesn't zero them.** The 4h reconcile is authoritative; if the SELL is still pending when reconcile fires, the row comes back from Alpaca with `current_stop=None` (preserved via COALESCE) and is skipped by the next `monitor_stops` tick.

## Notifications

[notifications/telegram.py](notifications/telegram.py) is the Telegram bridge. Three pieces:

- **`send_message(text)` never raises and never blocks the trading loop.** Telegram errors (network, invalid token, rate limit) are caught and logged; missing `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` silently falls back to log-only so dev runs without creds still work. Callers in [main.py](main.py) and [risk/manager.py](risk/manager.py) treat the return value as advisory — a notification failure must not cascade into a stop-monitor or signal-cycle failure.
- **python-telegram-bot v20+ is async.** Each call wraps the coroutine in `asyncio.run(bot.send_message(...))` rather than running our own event loop. Fine because traffic is low (~tens of messages/day); don't refactor to a long-lived loop unless volume grows.
- **`TelegramLogHandler` attaches to the *root* logger, not `cryptobot`.** Per [main.py](main.py) `setup_logging`, the file/stdout/Telegram handlers all live on root so log records from `execution.alpaca_executor`, `risk.manager`, `notifications.telegram`, etc. propagate up and reach all three sinks. If you re-add a handler to a named logger, set `propagate=False` on it or messages will duplicate.
- **Rate limit is per-`(logger_name, msg[:80])` over a 5-minute window.** Same exception from the same call site → one notification. Different messages or different loggers → independent windows. The dedupe map is module-global and thread-locked; `_reset_rate_limit_for_tests()` clears it for unit tests.
- **The handler filters its own records.** Without this, a Telegram-send failure would log an error from `notifications.telegram`, which the handler would try to forward via Telegram, which would fail and log again — infinite loop. Don't remove the `record.name.startswith("notifications.telegram")` early-return.
- **`build_daily_summary(repo)` is pure.** It reads from `repo.db.query_df` and `repo.get_open_positions_with_stops` and returns a string. Side-effect-free so tests fake the repo and assert on the rendered text; `send_daily_summary` is the thin wrapper that adds the Telegram call. At 00:00 UTC "today's trades" would be empty by definition, so the summary actually reports `[now-24h, now)` — the day that just closed.
- **Daily summary fires at 00:00:30 UTC.** That's 25 seconds after the `equity_snapshot` cron at `:00:05`, so the "Equity:" line in the report reflects the freshly-taken snapshot. Don't move either job onto exactly `:00:00` — APScheduler can serialize them in either order, and an off-by-one second gives the snapshot deterministic priority.

## Market data

[data/alpaca_data.py](data/alpaca_data.py) `AlpacaDataClient.get_bars()` returns a flat frame with columns `[ts, open, high, low, close, volume, trade_count, vwap]` and tz-aware UTC `ts` — it flattens alpaca-py's `(symbol, timestamp)` MultiIndex and backfills missing `trade_count`/`vwap` columns (some pairs omit them). Downstream code (strategy, repository) assumes this exact shape; if you call `CryptoHistoricalDataClient` directly elsewhere, replicate the flattening or you'll silently break `Repository.insert_bars`.

`CryptoHistoricalDataClient` works without API keys (free historical data); `TradingClient` does not — `AlpacaDataClient` lazy-instantiates the trading client and raises if keys are missing only when account/order methods are called.

## Build roadmap

The plan is structured as 10 sequential "sessions" (§6 of initial.md). Each session has a verbatim Claude prompt, a deliverable, and a test. When asked to "do session N", treat the prompt in §6 as the spec and the surrounding plan as constraints. Don't reorder sessions — session N depends on N-1 (e.g., backtest needs historical data already in SQL).

## Realistic expectations to preserve in user-facing output

The plan repeatedly emphasizes that this is **educational, not financial advice**, that 74–89% of retail traders lose money, and that 20–40% drawdowns are expected. Don't strip these caveats when generating user-facing READMEs, dashboards, or summaries — they are intentional.
