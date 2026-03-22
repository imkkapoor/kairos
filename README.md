# Kairos — Algorithmic Paper Trading System

Kairos is a monorepo Python + Next.js system that runs trading strategies on US (S&P 500) and Canadian (TSX 60) equities using paper money. All execution is self-simulated in TimescaleDB — no broker account needed. Every trade is logged with a plain-English reason so you can shadow the bot's decisions with real money if you choose.

---

## Tech Stack

| Layer       | Technology                                      |
|-------------|------------------------------------------------|
| Data store  | TimescaleDB (PostgreSQL 16 + time-series ext.) |
| Backend     | Python 3.12+, pandas, yfinance, SQLAlchemy     |
| Scheduler   | `schedule` + `zoneinfo` (ET-aware)             |
| Dashboard   | Next.js (Phase 6, not yet built)               |
| Container   | Docker Compose (DB only — Python runs locally) |

---

## First-Time Setup (4 commands)

```bash
make install   # create backend/.venv and install dependencies
make up        # start TimescaleDB container (kairos_db)
make setup     # init schema, seed ~560 tickers, backfill 5yr OHLCV
make run       # start the weekday scheduler
```

---

## Makefile Targets

| Target       | Description                                                 |
|--------------|-------------------------------------------------------------|
| `install`    | Create `backend/.venv`, upgrade pip, install requirements   |
| `up`         | Start TimescaleDB container in background                   |
| `down`       | Stop the container                                          |
| `logs`       | Stream Docker container logs                                |
| `setup`      | Init schema, seed watchlist, backfill 5yr OHLCV data        |
| `run`        | Start the weekday scheduler (data + strategy stubs)         |
| `fetch`      | Run a one-off incremental OHLCV update for all tickers      |
| `shell-db`   | Open an interactive `psql` session inside the container     |
| `reset-db`   | **DESTRUCTIVE** — wipe all data and recreate the database   |
| `help`       | Print this target list with first-time flow                 |

---

## Project Structure

```
kairos/
├── backend/
│   ├── db/
│   │   ├── init.sql           ← schema (auto-run by Docker on first start)
│   │   └── connection.py      ← ONLY file that reads/writes the DB
│   ├── data/
│   │   └── fetcher.py         ← yfinance downloader with rate limiting
│   ├── strategies/            ← Phase 2
│   ├── simulator/             ← Phase 3
│   ├── backtesting/           ← Phase 4
│   ├── ai/                    ← Phase 5
│   ├── notifier/              ← Phase 6
│   ├── scheduler.py           ← long-running weekday job runner
│   ├── setup.py               ← one-time init script
│   ├── requirements.txt
│   └── .env.example
├── dashboard/                 ← Phase 6 (Next.js, empty)
├── docker-compose.yml
├── Makefile
└── README.md
```

---

## Database

**Connection** (after `make up`):

```
Host:     localhost
Port:     5432
Database: kairos
User:     kairos
Password: kairos_dev
```

Or use `make shell-db` to open a `psql` shell directly.

### Tables

| Table                  | Type        | Description                           |
|------------------------|-------------|---------------------------------------|
| `price_data`           | hypertable  | OHLCV bars (daily + intraday)         |
| `indicators`           | hypertable  | Computed technical indicators         |
| `signals`              | regular     | Strategy-generated trade signals      |
| `trades`               | regular     | Simulated paper trade executions      |
| `portfolio_snapshots`  | hypertable  | Point-in-time portfolio state         |
| `watchlist`            | regular     | Universe of tracked tickers           |
| `fetch_log`            | regular     | Audit log for every yfinance fetch    |

### Key Columns

- **indicators**: `rsi_14`, `ma_50`, `ma_200`, `ema_20`, `bb_upper/mid/lower`, `atr_14` (Phase 3), `adx_14` (Phase 2), `volume_sma`
- **trades**: `stop_loss`, `take_profit`, `signal_strength` (all required by Phase 3)

---

## How to Add a Ticker

```python
from db.connection import add_to_watchlist
from data.fetcher import backfill

add_to_watchlist("NVDA", name="NVIDIA Corporation", sector="Technology", market="US")
backfill("NVDA", years=5)
```

---

## Scheduler Jobs (Eastern Time, Weekdays Only)

| Time (ET) | Job                                  |
|-----------|--------------------------------------|
| 07:00     | Morning digest — Phase 6 placeholder |
| 17:00     | `update_all()` — incremental OHLCV   |
| 17:15     | Strategy scan — Phase 2 placeholder  |
| 17:20     | Simulator — Phase 3 placeholder      |
| Every hr  | DB health check (runs daily)         |

---

## Phase Status

| Phase | Description            | Status      |
|-------|------------------------|-------------|
| 1     | Data pipeline          | ✅ Complete |
| 2     | Strategy engine        | 🔜 Next     |
| 3     | Paper trade simulator  | ⬜ Pending  |
| 4     | Backtesting engine     | ⬜ Pending  |
| 5     | AI / sentiment layer   | ⬜ Pending  |
| 6     | Dashboard + notifier   | ⬜ Pending  |

---

## Python Isolation

All Python runs inside `backend/.venv`. The system Python is never invoked.  
The `Makefile` always calls `backend/.venv/bin/python` and `backend/.venv/bin/pip` explicitly.

## Timezone Contract

All timestamps stored in the DB are UTC. The dashboard converts to local time at display time only. `yfinance` returns ET-localised timestamps — `fetcher.py` converts them to UTC before every DB write.

