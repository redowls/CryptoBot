# Building a Crypto Trading Bot on Alpaca + SQL Server + Linux VPS: A Decision-Ready Build Plan

**TL;DR**
- Build the bot in **Python 3.11** using **alpaca-py 0.43.4** (released April 29, 2026 on PyPI) for paper trading first; deploy on a **Contabo or Hetzner VPS running Ubuntu 24.04 LTS**, run **SQL Server 2025 Express on the same VPS** (free; Linux-native; GA on Ubuntu 24.04 since CU1 announced by Microsoft Tech Community on January 16, 2026), and connect Python to SQL Server via **pyodbc + Microsoft ODBC Driver 18**. Indonesia IS on Alpaca's listed crypto-trading jurisdictions (Alpaca's "What regions support cryptocurrency trading?" page, "Currently (as of Oct 9, 2025), cryptocurrency trading is open to the following U.S. jurisdictions… and to the following international jurisdictions: … India, Indonesia, Isle of Man, Israel, Japan…"), so Alpaca is viable for live trading subject to KYC approval — if KYC is denied, **Binance** (binance.com) is the recommended fallback.
- The recommended strategy is a **Donchian-channel breakout (20-period entry / 10-period exit) on 4-hour BTC/USD and ETH/USD bars**, with a 200-EMA daily trend filter, ATR-based position sizing risking 1% of equity per trade, and a 2×ATR initial stop. This is the simplest professional-grade trend-following design with documented multi-decade edge (the Turtle System foundation) and is well-suited to crypto's persistent trends and 24/7 markets.
- Expect realistic returns in the **10–25% annual range in good years, with 20–40% drawdowns**, and a meaningful probability of losing money: a PiP World longitudinal study of 8 million traders across 295 million trades from 1998–2025 (reported by Hedge Fund Alpha) concluded "a staggering 74% to 89% of retail investors still lose money, and always have." Paper-trade for at least 30 days, then start live with $200–$500 maximum. This is educational, not financial advice.

---

## Key Findings

1. **Alpaca crypto IS available in Indonesia.** Alpaca's official support page (alpaca.markets/support/what-regions-support-cryptocurrency-trading), dated November 2025, explicitly lists Indonesia under "international jurisdictions" alongside India, Japan, Philippines, Malaysia, UAE, UK, Switzerland, Vietnam, and ~140 other countries. The page also states "All Paper accounts have access to Cryptocurrency trading," meaning paper testing works globally regardless of country. Live crypto access requires Alpaca's KYC; if rejected, **Binance** is the closest API-equivalent fallback (deepest liquidity, mature WebSocket + REST, ~6,000 req/min limits, `python-binance` SDK).
2. **Alpaca's crypto coverage is meaningful but US-centric.** 20+ assets across 50+ pairs (BTC, ETH, SOL, AVAX, DOGE, LINK, LTC, UNI, AAVE, etc.) versus USD, USDC, USDT, and BTC quote currencies. **Fees: 0.15% maker / 0.25% taker (Tier 1, ≤$100k 30-day volume)**, no margin, no shorting, 24/7 markets, $200k notional cap per order. Min order size dynamically calibrated to ~$1 notional equivalent.
3. **SQL Server runs natively on Linux now.** Per Microsoft Tech Community (Attinder Pal Singh, Jan 16, 2026): "We are excited to announce the General Availability (GA) of SQL Server 2025 on Red Hat Enterprise Linux (RHEL) 10 and Ubuntu 24.04, starting with the CU1 release" (CU1 = KB5074901, build 17.0.4005.7). Recommendation: install **SQL Server 2025 Express edition (free, up to 10 GB per DB)** on the same VPS as the bot — simplest, cheapest, no cross-network latency. The user installs **SSMS 20** on their Windows laptop and connects remotely over port 1433 for admin/queries.
4. **Tooling is mature.** `alpaca-py` is on PyPI at version **0.43.4** (released April 29, 2026, Apache-2.0 license, supports Python 3.8–3.14, author Rahul Chowdhury), with dedicated `CryptoHistoricalDataClient`, `CryptoDataStream`, and `TradingClient` classes. For backtesting use **vectorbt** (fastest, vectorized, free version maintained) or **backtesting.py** (simplest). For 24/7 operation use a **systemd service with Restart=always**.
5. **Strategy edge is real but modest.** Donchian breakouts on crypto produce ~30–40% win rates with average winners 3–5× average losers (per Altrady, 2025 BTC daily backtests covering 2017 bull, 2018 bear, 2020-21 bull, 2022 bear, 2023+ recovery). This is not a "get rich" system — it is a disciplined trend-capture system with a small statistical edge. The peer-reviewed paper Wiecki, Campbell, Lent & Stauth, *"All That Glitters Is Not Gold: Comparing Backtest and Out-of-Sample Performance on a Large Cohort of Trading Algorithms"* (Journal of Investing, vol. 25 no. 3, pp. 69–80, March 9, 2016; SSRN: 2745220) found across 888 algorithms that "commonly reported backtest evaluation metrics like the Sharpe ratio offer little value in predicting out of sample performance (R² < 0.025)."

---

## Details

### 1. Alpaca Crypto API — Capabilities & Limits (2025–2026)

**Supported assets (via `GET /v2/assets?asset_class=crypto`):** 20+ coins — AAVE, AVAX, BAT, BCH, BTC, CRV, DOGE, DOT, ETH, GRT, LINK, LTC, MKR, SHIB, SUSHI, UNI, USDC, USDT, XRP, XTZ, YFI, SKY — across ~52 pairs (USD, USDC, USDT, BTC quotes). Symbology uses slashes: `BTC/USD`, `ETH/USD`. Legacy `BTCUSD` form is auto-translated.

**Order types:** market, limit, stop_limit. **Time-in-force:** gtc, ioc. **No margin, no shorting, no OCO/bracket for crypto.** Max $200k notional per order. Fractional orders supported via `qty` or `notional`.

**Fees (effective April 2023, current):** Tier 1 (≤$100k 30-day volume) — **maker 0.15% / taker 0.25%**. Tier 2 (>$100k) — 0.12% / 0.22%. Charged on the credited side per trade.

**Endpoints:**
- Trading (live): `https://api.alpaca.markets/v2/orders`, `/v2/positions`, `/v2/account`
- Trading (paper): `https://paper-api.alpaca.markets/v2/...`
- Market data: `https://data.alpaca.markets/v1beta3/crypto/us/...` for bars, quotes, trades, orderbooks, snapshots, latest-bars
- WebSocket: `wss://stream.data.alpaca.markets/v1beta3/crypto/us` for real-time bars/quotes/trades; `wss://api.alpaca.markets/stream` for `trade_updates` (order events)

**Rate limits:** 200 requests/minute per API key (returns HTTP 429 on excess). WebSocket: typically one concurrent stream connection per account.

**Python SDK (alpaca-py 0.43.4, April 29, 2026):**
```bash
pip install alpaca-py==0.43.4
```
Key classes: `TradingClient`, `CryptoHistoricalDataClient`, `CryptoDataStream`, `CryptoBarsRequest`, `MarketOrderRequest`, `LimitOrderRequest`. Note: `CryptoHistoricalDataClient` does NOT require API keys for historical data. The older `alpaca-trade-api` is deprecated.

**Indonesia availability:** Confirmed on Alpaca's regions page (November 2025). All paper accounts have crypto access regardless of country. Live account KYC for Indonesia is handled case-by-case via Alpaca support (the old static country whitelist was replaced in Feb 2026 by a contact-support page that now reads: "Please contact support … for more information regarding whether your country is supported.").

**Fallback if KYC denied:** Use **Binance** (binance.com) — best API quality among Indonesia-accessible exchanges, with `python-binance` SDK, REST + WebSocket, free market data, well-documented. Bappebti-compliant alternative: **Tokocrypto** (Binance-owned, Indonesian-licensed) or **Indodax** (IDR pairs, mature REST + WebSocket API with HMAC-SHA512 auth, docs at github.com/btcid/indodax-official-api-docs). For this build, **proceed with Alpaca paper trading first**; if Alpaca live KYC is denied later, port the strategy to `python-binance` — the strategy logic is identical, only the order/data adapter changes.

### 2. Trading Strategy — Donchian Breakout (Turtle-Lite, Crypto-Adapted)

**Why Donchian over alternatives:**

| Strategy | Pro | Con | Verdict |
|---|---|---|---|
| **Donchian breakout (chosen)** | Mechanical, transparent, multi-decade edge in trending markets, crypto trends persist for weeks | Low win rate (~35%), whipsaws in choppy regimes | **Best simple system with real edge** |
| EMA crossover | Trivial to code | Massively over-fit; small effective edge after costs | Skip — popular but weak |
| Bollinger Z-score mean reversion | Works in range markets | Catastrophic losses in trends; crypto trends a lot | Skip for primary system |
| MACD/RSI momentum | Easy to code | Lagging, no clear exit logic | Skip |
| ML (LSTM, XGBoost) | Potentially powerful | Overkill for a "simple bot"; severe overfitting risk; needs huge data | **Skip for v1** |
| Hybrid regime detection | Theoretically best | Too complex for one-session builds | Defer to v2 |

**Citation/evidence base:** The Donchian breakout is the foundation of the original **Turtle Trading System** (Richard Dennis, 1983). Backtests on BTC daily candles since 2017 show the 20/55 system "producing positive returns through both 2017 bull, 2018 bear, 2020-21 bull, 2022 bear, and 2023+ recovery" with win rates 30–40% and average winners 3–5× average losers (Altrady, 2025). On Nasdaq/Gold daily data 1990–2025, a 50-high/40-low Donchian system delivered ~5,000% cumulative return with average winner +62%, average loser –12%, win rate just under 49% (Financial Wisdom TV backtest). Crypto suits Donchian uniquely: 24/7 markets remove gap risk, and persistent trends mean wide channels with extended breakouts.

**Exact strategy spec:**
- **Pairs:** `BTC/USD` and `ETH/USD` (most liquid, tightest spreads on Alpaca).
- **Timeframe:** 4-hour bars. Rationale: avoids 1-minute noise/whipsaws, generates 1–3 signals per pair per month, fits a hobbyist bot polling cadence, and 4h Donchian breakouts on BTC/ETH have well-documented edge.
- **Indicators:**
  - Donchian upper channel (20-period high of prior 20 closed bars; SHIFT BY ONE BAR to avoid look-ahead bias).
  - Donchian lower channel (10-period low) — used for exit.
  - 200-period EMA on the **daily** timeframe — regime filter.
  - 14-period ATR on 4h — for stops and position sizing.
- **Entry (long-only, v1):** Enter market buy on a 4h close above the prior 20-bar Donchian high, **only if** the daily close > daily 200-EMA (trend filter). No shorts in v1 (Alpaca doesn't support crypto shorts anyway).
- **Initial stop loss:** 2 × ATR(14) below entry price. Track in code; if 4h close ≤ stop, market-sell to flat.
- **Exit (trailing):** Exit on a 4h close below the 10-period Donchian low (turtle-style trailing exit). Whichever triggers first: stop or trailing exit.
- **Take profit:** None fixed. Let winners run; trail with the 10-bar low.
- **Position sizing (volatility-adjusted, 1% risk):**
  ```
  risk_per_trade_usd = account_equity * 0.01
  position_size_units = risk_per_trade_usd / (2 * ATR_at_entry)
  position_size_usd = position_size_units * entry_price
  # Cap at 25% of equity per pair to avoid concentration
  ```
- **Risk caps:** Max 2 open positions (one per pair). Max daily loss 3% of equity → halt new entries for 24h. Max drawdown circuit-breaker at 15% peak-to-trough → stop trading, alert user.

### 3. Architecture & Tech Stack

**Language: Python 3.11+.** Reasons: (a) alpaca-py is Python-only; (b) richest ecosystem for trading (pandas, numpy, vectorbt, ta-lib, backtesting.py); (c) easiest for Claude to write/iterate; (d) pyodbc is mature for SQL Server.

**Components:**
- **Scheduler:** `APScheduler` (BackgroundScheduler) with a single 4h-aligned cron job (`0 */4 * * *`) for signal evaluation. Plus a separate 1-minute job for position/stop monitoring. Avoid OS-level `cron` because we want everything in-process for state management.
- **Market data:** REST polling via `CryptoHistoricalDataClient.get_crypto_bars()` is sufficient for 4h strategy. WebSocket (`CryptoDataStream`) is overkill at 4h timeframe but optional for position monitoring.
- **Strategy module:** `strategy/donchian.py` — pure functions that take a DataFrame and return a `Signal` dataclass (action, size, stop_price).
- **Order execution:** `execution/alpaca_executor.py` wraps `TradingClient.submit_order()` with retry/idempotency (use `client_order_id` to dedupe).
- **Risk/position manager:** `risk/manager.py` enforces daily loss, drawdown, max positions; reconciles SQL state with Alpaca's `/positions` on every cycle.
- **Database layer:** `db/repository.py` using **pyodbc** + Microsoft ODBC Driver 18 for SQL Server. Connection string:
  ```python
  conn_str = (
      "Driver={ODBC Driver 18 for SQL Server};"
      "Server=localhost,1433;"
      "Database=cryptobot;"
      "UID=sa;PWD=<password>;"
      "TrustServerCertificate=yes;"
  )
  ```
- **Logging:** Python `logging` to both file (`logs/bot.log`, rotating 10MB×5) and SQL `logs` table for ERROR-level. Mirror to stdout for `journalctl -u cryptobot`.
- **Config:** `.env` (via `python-dotenv`) for API keys + DB password; `config.yaml` for strategy parameters (so you can tune without code changes).
- **Notifications:** Telegram bot — free, easy. Send: trade fills, stops hit, errors, daily P&L summary at 00:00 UTC.

**Where SQL Server lives:** **Install SQL Server 2025 Express on the Linux VPS**, not on the user's Windows laptop. Reasons: (1) bot needs always-on DB access; (2) zero cross-network latency; (3) Express edition is free up to 10 GB per DB — far more than this bot will ever use; (4) GA on Ubuntu 24.04 since CU1 (Jan 16, 2026). The user installs **SSMS 20** on their Windows laptop and connects remotely to `<vps-ip>,1433` for browsing/queries. Open port 1433 in UFW only to the user's home IP (security note for later).

### 4. Database Schema (SQL Server)

```sql
CREATE DATABASE cryptobot;
GO
USE cryptobot;
GO

-- 1. Historical OHLCV bars
CREATE TABLE market_data (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    symbol          VARCHAR(20)    NOT NULL,
    timeframe       VARCHAR(10)    NOT NULL,   -- '4Hour', '1Day'
    ts              DATETIME2(0)   NOT NULL,   -- UTC bar start time
    open_px         DECIMAL(20,8)  NOT NULL,
    high_px         DECIMAL(20,8)  NOT NULL,
    low_px          DECIMAL(20,8)  NOT NULL,
    close_px        DECIMAL(20,8)  NOT NULL,
    volume          DECIMAL(28,8)  NOT NULL,
    trade_count     INT            NULL,
    vwap            DECIMAL(20,8)  NULL,
    created_at      DATETIME2(3)   DEFAULT SYSUTCDATETIME(),
    CONSTRAINT uq_market_data UNIQUE (symbol, timeframe, ts)
);
CREATE INDEX ix_market_data_sym_tf_ts ON market_data(symbol, timeframe, ts DESC);

-- 2. Signals generated by the strategy
CREATE TABLE signals (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    ts              DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    symbol          VARCHAR(20)    NOT NULL,
    strategy        VARCHAR(50)    NOT NULL,   -- 'donchian_20_10'
    side            VARCHAR(5)     NOT NULL,   -- 'BUY' | 'SELL'
    signal_price    DECIMAL(20,8)  NOT NULL,
    atr             DECIMAL(20,8)  NULL,
    proposed_qty    DECIMAL(28,8)  NULL,
    proposed_stop   DECIMAL(20,8)  NULL,
    notes           NVARCHAR(500)  NULL
);
CREATE INDEX ix_signals_ts ON signals(ts DESC);

-- 3. Orders sent to Alpaca
CREATE TABLE orders (
    id                  BIGINT IDENTITY(1,1) PRIMARY KEY,
    client_order_id     VARCHAR(64)    NOT NULL UNIQUE,
    alpaca_order_id     VARCHAR(64)    NULL,
    signal_id           BIGINT         NULL FOREIGN KEY REFERENCES signals(id),
    symbol              VARCHAR(20)    NOT NULL,
    side                VARCHAR(5)     NOT NULL,
    type                VARCHAR(20)    NOT NULL,   -- 'market', 'limit', 'stop_limit'
    qty                 DECIMAL(28,8)  NOT NULL,
    limit_price         DECIMAL(20,8)  NULL,
    status              VARCHAR(20)    NOT NULL,   -- 'new','filled','canceled','rejected'
    submitted_at        DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    filled_qty          DECIMAL(28,8)  NULL,
    filled_avg_price    DECIMAL(20,8)  NULL,
    filled_at           DATETIME2(3)   NULL,
    raw_response        NVARCHAR(MAX)  NULL
);
CREATE INDEX ix_orders_status_ts ON orders(status, submitted_at DESC);

-- 4. Filled trades / round-trip P&L
CREATE TABLE trades (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    symbol          VARCHAR(20)    NOT NULL,
    entry_order_id  BIGINT         FOREIGN KEY REFERENCES orders(id),
    exit_order_id   BIGINT         FOREIGN KEY REFERENCES orders(id),
    entry_ts        DATETIME2(3)   NOT NULL,
    exit_ts         DATETIME2(3)   NULL,
    entry_price     DECIMAL(20,8)  NOT NULL,
    exit_price      DECIMAL(20,8)  NULL,
    qty             DECIMAL(28,8)  NOT NULL,
    pnl_usd         DECIMAL(20,8)  NULL,
    pnl_pct         DECIMAL(10,4)  NULL,
    fees_usd        DECIMAL(20,8)  NULL,
    status          VARCHAR(10)    NOT NULL DEFAULT 'OPEN' -- OPEN | CLOSED
);
CREATE INDEX ix_trades_status ON trades(status);

-- 5. Currently open positions (mirror of Alpaca)
CREATE TABLE positions (
    symbol          VARCHAR(20)    PRIMARY KEY,
    qty             DECIMAL(28,8)  NOT NULL,
    avg_entry_price DECIMAL(20,8)  NOT NULL,
    current_stop    DECIMAL(20,8)  NULL,
    opened_at       DATETIME2(3)   NOT NULL,
    last_updated    DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME()
);

-- 6. Daily account equity snapshots
CREATE TABLE account_snapshots (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    ts              DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME(),
    equity          DECIMAL(20,2)  NOT NULL,
    cash            DECIMAL(20,2)  NOT NULL,
    buying_power    DECIMAL(20,2)  NOT NULL,
    portfolio_value DECIMAL(20,2)  NOT NULL
);
CREATE INDEX ix_snap_ts ON account_snapshots(ts DESC);

-- 7. Bot operational logs (errors and key events)
CREATE TABLE logs (
    id          BIGINT IDENTITY(1,1) PRIMARY KEY,
    ts          DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    level       VARCHAR(10)    NOT NULL,    -- INFO/WARN/ERROR
    module      VARCHAR(50)    NULL,
    message     NVARCHAR(MAX)  NOT NULL,
    exception   NVARCHAR(MAX)  NULL
);
CREATE INDEX ix_logs_ts_lvl ON logs(ts DESC, level);
```

### 5. VPS Deployment

**Recommended specs (single-pair bot + SQL Server):**
- **2 vCPU, 4 GB RAM, 80 GB SSD** — comfortable headroom for SQL Server (minimum 2 GB RAM for SQL Server alone) plus the Python bot.

**Recommended provider (ranked):**

| Provider | Plan | Monthly | Notes |
|---|---|---|---|
| **Contabo Cloud VPS S** | 4 vCPU, 8 GB, 200 GB SSD, 32 TB bandwidth | **$6.99** (setup fee waived on 3-month prepay; per BestUSAVPS.com Sept 2025–Mar 2026 benchmark review) | Best $/spec; Singapore region for Indonesia latency |
| Hetzner CX22 | 2 vCPU, 4 GB, 40 GB | ~€4.90 (~$5.30) | Best CPU consistency; EU only |
| Vultr Cloud Compute Regular | 2 vCPU, 4 GB, 80 GB | $20 | Singapore region for Indonesia |
| DigitalOcean Basic Droplet | 2 vCPU, 4 GB, 80 GB | $24 | Most polished UX; Singapore region |
| AWS Lightsail | 2 vCPU, 4 GB, 80 GB | $24 | Use if already on AWS |

**Pick Contabo Cloud VPS S in Singapore** (~$6.99/mo) — lowest cost, generous RAM for SQL Server, low latency from Indonesia to Singapore region.

**OS:** Ubuntu 24.04 LTS (Microsoft SQL Server 2025 has been GA on this distro since CU1, Jan 16, 2026).

**24/7 operation: systemd service.** Simpler than Docker for this use case; auto-restart on crash, on-boot startup, logs flow to journalctl.

```ini
# /etc/systemd/system/cryptobot.service
[Unit]
Description=Crypto Trading Bot
After=network-online.target mssql-server.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/cryptobot
EnvironmentFile=/home/trader/cryptobot/.env
ExecStart=/home/trader/cryptobot/venv/bin/python -u main.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Deployment workflow:** `git clone` initially; thereafter `git pull && sudo systemctl restart cryptobot`. Use a private GitHub repo with `.env` in `.gitignore`.

**Monitoring (bare minimum):**
- `journalctl -u cryptobot -f` for live logs
- File log at `logs/bot.log` (rotating)
- Telegram daily P&L message at 00:00 UTC
- Telegram instant alert on any uncaught exception

**Security (deferred per user request, but bare minimum):**
- `.env` file with permissions `chmod 600`, never commit to git
- UFW firewall: allow only SSH (22) and SQL Server (1433) from the user's home IP. Do not expose port 1433 to the internet.
- Use SSH keys, disable password auth (later).

### 6. Step-by-Step Build Plan (10 Sessions)

Each session is sized to ~1–2 hours with Claude generating the code. Run each session in a fresh Claude conversation, paste the prompt verbatim, and provide context files when asked.

---

**Session 1 — Project scaffolding & local Python env**
- **Pre-session:** Install Python 3.11 on local Windows machine + Git + VS Code. Create empty private GitHub repo `cryptobot`.
- **Claude prompt:**
  > "Create a Python 3.11 project scaffold for a crypto trading bot named `cryptobot`. Folder structure: `config/`, `db/`, `data/`, `strategy/`, `execution/`, `risk/`, `notifications/`, `tests/`, `logs/`. Include `requirements.txt` with: alpaca-py==0.43.4, pyodbc, pandas, numpy, APScheduler, python-dotenv, pyyaml, python-telegram-bot, vectorbt. Include `.env.example`, `.gitignore` (exclude .env, logs/, venv/), `README.md`, and a stub `main.py` that loads config and prints 'Bot starting...'. Add a `config/config.yaml` with strategy parameters: donchian_entry=20, donchian_exit=10, atr_period=14, atr_stop_mult=2.0, risk_per_trade=0.01, max_positions=2, pairs=['BTC/USD','ETH/USD'], timeframe='4Hour'."
- **Deliverable:** Project on GitHub, `python main.py` prints startup line.
- **Test:** `git clone`, `pip install -r requirements.txt`, `python main.py` works.

---

**Session 2 — Alpaca paper account + data fetcher**
- **Pre-session:** Sign up at alpaca.markets, generate paper API keys, paste into `.env`.
- **Claude prompt:**
  > "Build `data/alpaca_data.py` using alpaca-py 0.43.4. Create class `AlpacaDataClient` that wraps `CryptoHistoricalDataClient`. Method `get_bars(symbol: str, timeframe: TimeFrame, lookback_days: int) -> pd.DataFrame` returning columns: ts, open, high, low, close, volume, trade_count, vwap (UTC, sorted ascending). Method `get_latest_quote(symbol)`. Method `get_account_info()` using `TradingClient(paper=True)`. Write `tests/test_data.py` that fetches last 30 days of BTC/USD 4Hour bars and prints summary stats. Load keys from `.env` via python-dotenv."
- **Deliverable:** Working data fetcher.
- **Test:** Run test, verify ~180 bars returned for 30 days, prices look sane.

---

**Session 3 — SQL Server install + schema**
- **Pre-session:** Provision Contabo VPS (Ubuntu 24.04, Singapore region). SSH in as root, create user `trader`, install SQL Server 2025 Express following Microsoft Learn quickstart (`sudo apt-get install -y mssql-server` then `sudo /opt/mssql/bin/mssql-conf setup`, choose Express edition #3, set sa password). Install msodbcsql18 driver. Install SSMS 20 on Windows. Connect SSMS to `<vps-ip>,1433`. Open port 1433 in UFW from your home IP only.
- **Claude prompt:**
  > "Generate a single SQL script `db/schema.sql` that creates database `cryptobot` and all seven tables: market_data, signals, orders, trades, positions, account_snapshots, logs — using the exact schema in the build plan. Also create a Python module `db/connection.py` with class `Database` that uses pyodbc + ODBC Driver 18, reads connection settings from `.env` (DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD), exposes `get_connection()` context manager, `execute(sql, params)`, and `query_df(sql, params) -> pd.DataFrame`. Include a `db/repository.py` with methods: `insert_bars(df)`, `insert_signal(...)`, `insert_order(...)`, `update_order_status(...)`, `upsert_position(...)`, `insert_snapshot(...)`, `log(level, module, msg, exc=None)`. Use parameterized queries throughout."
- **Deliverable:** Schema applied; DB module imports cleanly.
- **Test:** Run schema.sql in SSMS. `python -c "from db.connection import Database; Database().execute('SELECT 1')"` returns success.

---

**Session 4 — Historical data ingestion**
- **Pre-session:** None.
- **Claude prompt:**
  > "Create `data/ingest.py` with function `backfill_history(symbol, timeframe, days_back)` that fetches bars via AlpacaDataClient and upserts into `market_data` using the (symbol, timeframe, ts) unique constraint (use SQL Server MERGE). Add a CLI: `python -m data.ingest --symbol BTC/USD --timeframe 4Hour --days 365`. Print summary: bars fetched, inserted, skipped (duplicates). Handle pagination if the SDK doesn't auto-paginate."
- **Deliverable:** 1 year of historical 4h bars for BTC/USD and ETH/USD in SQL Server.
- **Test:** Run for both pairs, then `SELECT symbol, COUNT(*) FROM market_data GROUP BY symbol` in SSMS. Expect ~2,200 bars per pair.

---

**Session 5 — Strategy & signal generation**
- **Claude prompt:**
  > "Build `strategy/donchian.py`. Class `DonchianStrategy` configured from config.yaml. Method `compute_indicators(df: pd.DataFrame) -> pd.DataFrame` adds: donchian_high (rolling max of high, 20 periods, SHIFTED BY 1 BAR to avoid lookahead), donchian_low (rolling min of low, 10, shifted by 1), atr (14-period Wilder's ATR). Method `generate_signal(df_4h: pd.DataFrame, df_daily: pd.DataFrame, current_position: dict | None) -> Signal | None`. Logic: if no position and last 4h close > donchian_high AND last daily close > daily 200 EMA → BUY signal with stop = entry - 2*ATR. If position open and (close < donchian_low OR close < current_stop) → SELL signal. Define `Signal` dataclass with fields: symbol, side, price, stop, atr, reason. Write `tests/test_strategy.py` that loads bars from SQL Server and prints all historical signals."
- **Deliverable:** Pure-function strategy logic.
- **Test:** Run test; verify signal count and dates align with chart inspection in TradingView.

---

**Session 6 — Backtest module**
- **Claude prompt:**
  > "Build `backtest/runner.py` using vectorbt. Function `run_backtest(symbol, start_date, end_date, initial_cash=10000, fee_pct=0.0025)` that: pulls bars from SQL Server, computes Donchian entries/exits using the same strategy logic from session 5, simulates with vectorbt's `Portfolio.from_signals` including the 0.25% taker fee, 0.05% slippage, and ATR-based position sizing (1% risk per trade). Output: total return, CAGR, Sharpe, Sortino, max drawdown, win rate, profit factor, average winner/loser ratio, number of trades. Save equity curve to `backtest/results/{symbol}_{date}.png`. CLI: `python -m backtest.runner --symbol BTC/USD --start 2022-01-01 --end 2025-01-01`."
- **Deliverable:** Backtest reports for BTC/USD and ETH/USD over 2022–2025.
- **Test:** Visually inspect equity curve; check Sharpe > 0.5 and max DD < 40% for reasonable parameters.

---

**Session 7 — Paper order execution**
- **Claude prompt:**
  > "Build `execution/alpaca_executor.py`. Class `Executor` wraps `TradingClient(paper=True)`. Method `submit_market_order(symbol, qty, side, client_order_id)` returns the order dict; persists to `orders` table via repository. Method `cancel_all_open_orders(symbol)`. Method `get_position(symbol) -> dict | None`. Method `reconcile_positions()` — pulls Alpaca positions, updates SQL `positions` table. Generate `client_order_id` as `f'{symbol.replace(\"/\",\"\")}-{datetime.utcnow().strftime(\"%Y%m%d%H%M%S\")}-{uuid.uuid4().hex[:6]}'`. Wire it in to `main.py`: scheduler runs `signal_cycle()` every 4 hours on the hour; if a Signal is returned, executor submits market order, logs to DB, sends Telegram notification."
- **Deliverable:** Bot places paper orders end-to-end.
- **Test:** Manually create a synthetic signal in code; verify order appears in Alpaca paper dashboard and in `orders` table.

---

**Session 8 — Risk manager & stop monitoring**
- **Claude prompt:**
  > "Build `risk/manager.py`. Class `RiskManager` enforces: (1) max_positions cap; (2) max_daily_loss_pct halts new entries until next UTC day; (3) max_drawdown_pct triggers KILL_SWITCH that closes all positions and exits the process; (4) volatility position sizing per the strategy spec. Add a 1-minute scheduler job `stop_monitor()` in main.py that: for each open position, fetches latest price, checks if it's below the stored stop; if so, submits a market sell, updates `trades` table with pnl. Also: 1-hour `equity_snapshot()` job writes to `account_snapshots`."
- **Deliverable:** Bot honors stops and risk limits.
- **Test:** Manually shift a stop in DB above current price; verify bot exits the position within 60s.

---

**Session 9 — Logging, Telegram notifications, daily summary**
- **Claude prompt:**
  > "Build `notifications/telegram.py` with `send_message(text)` using python-telegram-bot's `Bot(token).send_message(chat_id, text)`. Configure via TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars. Wire alerts: on every order fill (entry/exit), on every error logged at ERROR level (rate-limited to max 1 per 5 minutes per error type), and a daily 00:00 UTC summary that queries: today's trades, P&L, current open positions, equity vs start of week. Add a top-level try/except in main.py that catches uncaught exceptions, logs them to DB, sends Telegram, and re-raises so systemd restarts."
- **Deliverable:** All key events visible on user's phone.
- **Test:** Trigger a synthetic order; verify Telegram message arrives within 5 seconds.

---

**Session 10 — VPS deployment as systemd service + go-live ramp**
- **Pre-session:** None — VPS already provisioned in session 3.
- **Claude prompt:**
  > "Generate: (1) `deploy/cryptobot.service` systemd unit file (per the build plan template); (2) a bash script `deploy/install.sh` that creates the trader user, clones the repo, creates venv, installs requirements, copies the service file, runs daemon-reload, enables and starts the service; (3) a `deploy/update.sh` script: git pull, pip install -r requirements.txt, systemctl restart cryptobot; (4) a README section on log inspection (`journalctl -u cryptobot -f`, `tail -f logs/bot.log`); (5) a `scripts/healthcheck.py` that verifies DB connectivity, Alpaca API reachability, and last signal timestamp — exits non-zero if any fail."
- **Deliverable:** Bot running 24/7 on VPS in paper mode.
- **Test:** Run for 7 days continuous; verify uptime via `systemctl status cryptobot`; verify ≥1 signal cycle per 4 hours in logs even if no trades fired.

---

**Going live (after 30+ days paper trading and ≥1 round-trip trade in paper):**
- Apply for live crypto eligibility in Alpaca dashboard (sign crypto agreement, complete enhanced KYC).
- Once approved and funded with $200–$500, generate **live** API keys and swap them in `.env` (and flip `paper=False` in TradingClient config).
- Keep `risk_per_trade` at 1% and verify first live trade is under $5 notional impact.
- Do not increase capital until the bot has 60+ days of live operation with no critical bugs.

**Session 11 (optional iteration):**
- Add SOL/USD as third pair (only if backtest shows positive expectancy independently).
- Walk-forward optimize Donchian periods (test 10/5, 20/10, 55/20).
- Add regime filter: only trade when BTC realized vol > 30% annualized (avoid choppy summer 2023-style markets).
- Add second strategy (e.g., breakout-from-consolidation) running concurrently to diversify regime exposure.

### 7. User Checklist (Manual Steps)

- [ ] Sign up at alpaca.markets, generate **paper** API keys (free, instant)
- [ ] Provision Contabo Cloud VPS S (Singapore region, Ubuntu 24.04), ~$6.99/mo
- [ ] SSH into VPS; install SQL Server 2025 Express following Microsoft Learn quickstart for Ubuntu 24.04
- [ ] Install Microsoft ODBC Driver 18 on VPS (`msodbcsql18` package)
- [ ] Install SSMS 20 on local Windows machine; verify remote connection to `vps-ip,1433`
- [ ] Create database `cryptobot` (or let Session 3 script do it)
- [ ] Install Python 3.11 + git on VPS and local
- [ ] Create Telegram bot via @BotFather; note token + your chat_id
- [ ] Open private GitHub repo for the project
- [ ] Run all 10 sessions in order with Claude
- [ ] **Paper trade for ≥30 days** before considering live
- [ ] Apply for Alpaca live crypto eligibility (sign agreement in dashboard)
- [ ] Fund Alpaca live account with $200–500 only initially
- [ ] Set up daily check-in habit (5 min/day reviewing Telegram summary + SSMS query)

### 8. Risks, Realistic Expectations & Disclaimers

**Realistic ROI expectations:**
- **Modal outcome: small loss to small gain.** Most retail bots underperform buy-and-hold BTC. A solid Donchian implementation can target 10–25% annualized in favorable years, but losing years are common.
- A PiP World longitudinal study of 8 million traders, 295 million trades over 27 years (1998–2025), as reported by Hedge Fund Alpha, found that **"74% to 89% of retail investors still lose money, and always have."**
- Realistic first-year returns for **well-tested** bots: single-digit-to-low-teens percentages, before fees and slippage (industry surveys of 3Commas DCA and Bitsgap grid bots).
- **Expect 20–40% drawdowns** even with a winning long-run system. 2022 was brutal for trend systems.

**Common failure modes:**
1. **Overfitting** — backtest looks great, live performance dies. Wiecki, Campbell, Lent & Stauth (Journal of Investing, 2016) found across 888 algorithmic strategies that "commonly reported backtest evaluation metrics like the Sharpe ratio offer little value in predicting out of sample performance (R² < 0.025)." **Mitigation:** lock down only 2–3 parameters (Donchian entry/exit lengths, ATR multiplier); never optimize on the live-test sample.
2. **Slippage and fees** — 0.25% taker fee + slippage on round trip ≈ 0.6% drag per trade; on 4h timeframe with ~20 trades/yr, that's ~12% headwind. Backtest must include these.
3. **API downtime/rate limits** — Alpaca returns HTTP 429 above 200 req/min; build retry logic with exponential backoff.
4. **Black swan moves** — March 2020 crash, May 2022 LUNA collapse, November 2022 FTX collapse — bots get killed by gap-like moves. Stops can slip badly. Use position sizing that survives a -20% overnight move.
5. **Look-ahead bias** — the most common quant bug. Always shift indicators by one bar before signal evaluation (the strategy spec does this).
6. **Bot ran but you didn't watch it** — silent failures are worse than crashes. Telegram daily summary is essential.
7. **Regulatory** — Indonesian crypto trading via foreign brokers exists in a gray zone; OJK (since Jan 10, 2025, per Government Regulation No. 49/2024) now regulates crypto domestically. Tax: VAT and income tax on crypto since May 2022; new 0.21% seller tax on domestic exchanges vs. 1% on foreign platforms effective August 2025. Consult an Indonesian tax advisor.

**Strong recommendations (non-negotiable):**
1. **Paper trade for ≥30 days** with multiple round-trip trades before any real money.
2. **Start live with $200–$500 maximum**, not the entire trading budget.
3. **Do not touch the bot during a drawdown.** The discipline is the edge; if you override the system after losses, you're worse than no bot.
4. **Review weekly, not hourly.** Compulsive checking causes meddling, which destroys systematic strategies.

**Disclaimers:**
- This document is **educational, not financial advice**. Cryptocurrency is highly speculative; total loss is possible.
- All performance figures cited (Donchian historical results, retail-loss statistics, AI bot returns) are from third-party sources and **past performance does not guarantee future results**.
- The user is solely responsible for compliance with Indonesian tax/regulatory obligations (OJK Regulation No. 27 of 2024).
- Alpaca availability in Indonesia is current as of November 2025 but is subject to change at Alpaca's discretion; live account approval is not guaranteed and is handled case-by-case via Alpaca support.

---

## Recommendations

**Stage 1 — Build & Paper (Weeks 1–6):**
- Run sessions 1–10 over 2–3 weekends. Don't rush; verify each session works before moving on.
- After session 10, let the bot run in paper mode for 30+ days. **Threshold to advance:** at least 5 round-trip trades executed correctly, no manual intervention required, all Telegram summaries arrived, no uncaught exceptions, paper P&L within ±10% of what backtest predicted for the same period.

**Stage 2 — Live Micro (Weeks 7–14):**
- If Alpaca KYC approves Indonesia residency, fund $200–$500 live. **Threshold to scale up:** 8 weeks of live operation, ≥3 round-trip live trades, behavior matches paper trading.
- If Alpaca KYC denies, pivot to Binance — re-do session 7 with `python-binance` (1–2 days of work). All other modules (strategy, DB, risk, backtest, notifications) carry over unchanged.

**Stage 3 — Scale & Iterate (Month 4+):**
- Scale capital only after 60 days clean live operation. Threshold: scale by 2× when live Sharpe (4-month) > 0.7 AND max drawdown < 20%.
- Add walk-forward parameter testing (session 11+).
- Consider second strategy (mean reversion or breakout-from-consolidation) running concurrently to diversify regime exposure.

**Stop-loss on the entire project:** If after 6 months of live operation the bot is down >30% from peak equity, **stop trading and revisit strategy**. Don't martingale, don't increase size, don't override stops.

---

## Caveats

- The Donchian strategy has **multi-decade evidence on Nasdaq, Gold, and crypto**, but the publicly cited BTC 30–40% win rate and 3–5× winner/loser ratio come from blog backtests (Altrady 2025, Financial Wisdom TV, Mudrex), not peer-reviewed studies. Run your own backtest in session 6 and verify before trusting it.
- The Alpaca crypto regions list cited Indonesia "as of Oct 9, 2025" and Alpaca explicitly stated they "are working to broaden eligibility criteria to more jurisdictions" — this can change at any time. **Live KYC approval is a separate gate** and is not guaranteed.
- SQL Server 2025 GA on Ubuntu 24.04 with CU1 is recent (Jan 16, 2026). Stick to Express edition and standard schemas; avoid bleeding-edge features (native vector search, AI integrations) until you actually need them.
- vectorbt's **free version is in maintenance mode**; for advanced features (live trading hooks, parameter sweep dashboards) the PRO version is paid. The free version is fully sufficient for this build.
- The Hedge Fund Alpha / PiP World "74–89% lose money" figure aggregates equities and FX traders, not strictly crypto bots; the directional message — most retail loses money — holds, but exact percentage in this specific use case is unknown.
- All financial-return figures are illustrative; **no strategy guarantees profits**, and most retail algo trading attempts lose money over reasonable horizons. Build this as a learning exercise first, a profit center a distant second.