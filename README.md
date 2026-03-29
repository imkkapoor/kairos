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

## First-Time Setup

```bash
make install   # create backend/.venv and install dependencies
make up        # start TimescaleDB container (kairos_db)
make setup     # init schema, seed ~560 tickers, backfill 5yr OHLCV
make run       # start the weekday scheduler
```

---

## How It Works

Kairos follows a **scan → execute → manage** cycle across two markets (US + CA) with CAD as the base portfolio currency.

**Evening (previous day):**
`make fetch` pulls closing OHLCV → `make scan` computes indicators + generates signals with strength scores.

**Morning (9:31 AM ET):**
`run_morning()` loads last night's signals, fetches the **9:31 AM ET opening bar** for stock prices and the FX rate (USDCAD) in one batch yfinance call, sizes positions via ATR-based risk, and opens paper trades. Both fill prices and FX rates are pinned to the same 9:31 AM bar.

**Evening (5:25 PM ET):**
`run_evening()` reads closing prices from the DB (no yfinance call), checks stop-losses, take-profits, and trailing stops, and closes positions that hit exit criteria.

### Multi-Currency

| Market | Currency | FX Pair    |
|--------|----------|------------|
| US     | USD      | USDCAD=X   |
| CA     | CAD      | (base)     |

All portfolio values, position sizing, and P&L are computed in the base currency (`PORTFOLIO_CURRENCY`, default CAD). The FX rate is fetched at the same 9:31 AM cutoff as stock prices. Canadian tickers trade natively in CAD (fx_rate = 1.0).

---

## Run Patterns

### Option A: Scheduler (hands-off)

```bash
make run   # starts scheduler.py — runs indefinitely
```

The scheduler fires all jobs automatically on **weekdays only** (ET). On weekends and holidays, jobs are skipped — the process stays alive but does nothing.

| Time (ET) | Job                  | What it does                                |
|-----------|----------------------|---------------------------------------------|
| 07:00     | Morning digest       | Phase 6 placeholder                         |
| 09:31     | `run_morning()`      | Batch price + FX fetch → execute signals    |
| 17:00     | `update_all()`       | Incremental OHLCV for all tickers           |
| 17:15     | `run_daily_scan()`   | Compute indicators → generate signals       |
| 17:25     | `run_evening()`      | DB close prices → stop/TP/trailing checks   |
| Every hr  | DB health check      | Runs daily including weekends               |

### Option B: Manual Make Commands

Run individual steps whenever you want. Useful for testing or catching up after downtime.

```bash
make fetch              # pull latest OHLCV bars
make scan               # compute indicators + generate signals
make simulate           # run morning execution (9:31 AM bar prices + FX)
make simulate-evening   # run evening position management (DB close prices)
```

**Weekday during market hours** — the normal flow:
```bash
make fetch && make scan    # evening: pull data + generate signals
make simulate              # next morning: execute signals at 9:31 bar
make simulate-evening      # same evening: check stops/TPs with close
```

**Weekend / after hours** — safe to run, but:
- `make simulate` uses last Friday's 9:31 AM bar (yfinance returns the most recent trading day). Signals are loaded from the last weekday scan via `_last_scan_date_utc()`.
- `make simulate-evening` reads the latest close prices from the DB — whatever was last fetched.
- `make fetch` and `make scan` work fine — they just pull/compute with the latest available data.

---

## Makefile Targets

| Target             | Description                                                 |
|--------------------|-------------------------------------------------------------|
| `install`          | Create `backend/.venv`, upgrade pip, install requirements   |
| `up`               | Start TimescaleDB container in background                   |
| `down`             | Stop the container                                          |
| `logs`             | Stream Docker container logs                                |
| `setup`            | Init schema, seed watchlist, backfill 5yr OHLCV data        |
| `run`              | Start the weekday scheduler (runs indefinitely)             |
| `fetch`            | One-off incremental OHLCV update for all tickers            |
| `scan`             | One-off signal scan (indicators + signals)                  |
| `simulate`         | Morning execution (9:31 bar prices + FX, last weekday signals) |
| `simulate-evening` | Evening management (DB close prices, stop/TP checks)        |
| `shell-db`         | Open interactive `psql` session                             |
| `reset-db`         | **DESTRUCTIVE** — wipe all data and recreate the database   |

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
│   ├── strategies/
│   │   ├── scanner.py         ← daily signal scan orchestrator
│   │   ├── indicators.py      ← technical indicator computation
│   │   ├── rsi.py, momentum.py, macd.py, reversal.py, sector_rotation.py
│   │   └── regime.py          ← beta-weighted market regime filter
│   ├── simulator/
│   │   ├── simulator.py       ← morning + evening job orchestrator
│   │   ├── executor.py        ← signal → trade conversion, price + FX fetch
│   │   ├── portfolio.py       ← in-memory portfolio with risk limits
│   │   └── position_manager.py ← stop/TP/trailing stop checker
│   ├── scheduler.py           ← long-running weekday job runner
│   ├── setup.py               ← one-time init script
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
Host: localhost  Port: 5432  DB: kairos  User: kairos  Pass: kairos_dev
```
Or `make shell-db` for a `psql` shell.

### Tables

| Table                  | Type        | Description                           |
|------------------------|-------------|---------------------------------------|
| `price_data`           | hypertable  | OHLCV bars (daily + intraday)         |
| `indicators`           | hypertable  | Computed technical indicators         |
| `signals`              | regular     | Strategy-generated trade signals      |
| `trades`               | regular     | Paper trades (currency + fx_rate)     |
| `portfolio_snapshots`  | hypertable  | Point-in-time portfolio state (CAD)   |
| `watchlist`            | regular     | Universe of tracked tickers           |
| `fetch_log`            | regular     | Audit log for every yfinance fetch    |

### Key Columns

- **indicators**: `rsi_14`, `ma_50`, `ma_200`, `ema_20`, `bb_upper/mid/lower`, `atr_14`, `adx_14`, `volume_sma`, `macd_line`, `macd_signal`, `macd_hist`, `roc_20`
- **signals**: 5 strategies (rsi, momentum, macd, reversal, sector_rotation) with `strength` score
- **trades**: `fill_price` (9:31 AM bar), `currency`, `fx_rate`, `stop_loss`, `take_profit`

---

## Configuration (.env)

| Variable                | Default     | Notes                                |
|------------------------|-------------|--------------------------------------|
| `INITIAL_CAPITAL`       | 100000.0    | Starting cash in portfolio currency  |
| `PORTFOLIO_CURRENCY`    | CAD         | Base currency for all valuations     |
| `MARKET_OPEN_HOUR_ET`   | 9           | Hour for price/FX cutoff             |
| `MARKET_OPEN_MINUTE_ET` | 31          | Minute for price/FX cutoff           |
| `MAX_PORTFOLIO_RISK`    | 0.02        | Max risk per trade as % of portfolio |
| `MAX_POSITION_SIZE`     | 0.10        | Max single position as % of total    |
| `MAX_TOTAL_EXPOSURE`    | 0.80        | Max invested fraction                |
| `MAX_SECTOR_EXPOSURE`   | 0.30        | Max single sector fraction           |
| `MAX_OPEN_POSITIONS`    | 20          | Hard cap on concurrent positions     |
| `MIN_SIGNAL_STRENGTH`   | 0.10        | Signals below this are skipped       |

---

## Phase Status

| Phase | Description            | Status      |
|-------|------------------------|-------------|
| 1     | Data pipeline          | ✅ Complete |
| 2     | Strategy engine        | ✅ Complete |
| 3     | Paper trade simulator  | ✅ Complete |
| 4     | Backtesting engine     | ⬜ Pending  |
| 5     | AI / sentiment layer   | ⬜ Pending  |
| 6     | Dashboard + notifier   | ⬜ Pending  |

---

## Timezone Contract

All DB timestamps are UTC. The scheduler uses `zoneinfo("America/New_York")` for ET-aware scheduling. `yfinance` returns ET-localised timestamps — `fetcher.py` converts to UTC before every DB write.
