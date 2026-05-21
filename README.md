# cryptobot

A Python 3.11 crypto trading bot trading BTC/USD and ETH/USD via Alpaca, persisting state to SQL Server, with Telegram notifications. Paper-first; live trading is a separate gate after 30+ days of paper validation.

See [initial.md](initial.md) for the full build plan and [CLAUDE.md](CLAUDE.md) for architectural invariants.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux/macOS
pip install -r requirements.txt
cp .env.example .env           # fill in API keys
python main.py
```

Expected output: `Bot starting...` followed by the configured pairs and timeframe.

## Layout

- `config/` — `config.yaml` strategy parameters
- `data/` — Alpaca market data client and ingestion
- `db/` — SQL Server schema and repository
- `strategy/` — Donchian breakout signal generation
- `backtest/` — vectorbt backtest runner
- `execution/` — Alpaca order submission + position reconciliation
- `risk/` — position sizing, entry gates, kill switch, 1-min stop monitor
- `notifications/` — Telegram alerts (sync wrapper, ERROR-level log mirror, 00:00 UTC daily summary)
- `tests/` — unit and integration tests
- `logs/` — rotating file logs (gitignored)
- `deploy/` — systemd unit + install/update scripts for the VPS
- `scripts/` — operational helpers (e.g. `healthcheck.py`)

## VPS deployment

Prerequisites (one-time, per [initial.md](initial.md) §3, §5):
- Ubuntu 24.04 LTS, Python 3.11+, git
- SQL Server 2025 Express + ODBC Driver 18, with the `cryptobot` DB created via [db/schema.sql](db/schema.sql)
- UFW: port 22 open, 1433 restricted to your home IP

Install and run as a systemd service (as root on the VPS):

```bash
git clone <repo> /tmp/cryptobot-bootstrap
sudo REPO_URL=<repo> /tmp/cryptobot-bootstrap/deploy/install.sh
# fill in /home/trader/cryptobot/.env
sudo systemctl start cryptobot
```

Update later (as the `trader` user, from `/home/trader/cryptobot`):

```bash
./deploy/update.sh   # git pull --ff-only, refresh deps if requirements.txt changed, restart
```

## Log inspection

```bash
sudo systemctl status cryptobot           # current state + last few lines
journalctl -u cryptobot -f                # live tail (stdout/stderr from python -u)
journalctl -u cryptobot --since "1h ago"  # recent window
tail -f /home/trader/cryptobot/logs/bot.log  # rotating file log (10MB x 5)
```

For structured queries, `logs` and `signals` tables in SQL Server are authoritative — connect from SSMS on Windows or run `scripts/healthcheck.py` for a one-shot DB + Alpaca + last-signal probe (exits non-zero on failure, suitable for cron / uptime monitors).

## Status

Sessions 1–9 of 10 complete — scaffold, Alpaca data client, SQL Server schema/repository, historical ingest (1yr 4h + 600d daily for BTC/ETH actually loaded), Donchian strategy with full unit coverage, vectorbt backtest, Alpaca executor wired into an APScheduler 4h loop in `main.py`, RiskManager with 1-min stop monitor and 1-hr equity snapshot job, real Telegram bridge with rate-limited ERROR mirror and 00:00 UTC daily summary plus top-level crash handler. Session 10 (systemd deploy) remains. See [initial.md](initial.md) §6 for the full session roadmap.
