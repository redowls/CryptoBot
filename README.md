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
- `risk/` — position sizing (stop monitoring, kill switch pending Session 8)
- `notifications/` — Telegram alerts *(scaffold only — Session 9)*
- `tests/` — unit and integration tests
- `logs/` — rotating file logs (gitignored)

## Status

Sessions 1–7 of 10 complete — scaffold, Alpaca data client, SQL Server schema/repository, historical ingest (1yr 4h + 600d daily for BTC/ETH actually loaded), Donchian strategy with full unit coverage, vectorbt backtest, Alpaca executor wired into an APScheduler 4h loop in `main.py`. Sessions 8–10 (risk manager + stop monitor, Telegram, systemd deploy) remain. See [initial.md](initial.md) §6 for the full session roadmap.
