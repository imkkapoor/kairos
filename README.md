# Kairos — Algorithmic Paper Trading System

Monorepo Python + Next.js system running 5 trading strategies on ~560 US (S&P 500) and Canadian (TSX 60) equities using paper money. Self-simulated in TimescaleDB — no broker account needed. Every trade is logged with a plain-English reason.

See [Allocation.md](Allocation.md) for the full breakdown of signal strength, z-score normalisation, and position sizing.

---

## Tech Stack

| Layer      | Technology                                      |
|------------|-------------------------------------------------|
| Data store | TimescaleDB (PostgreSQL 16 + time-series ext.)  |
| Backend    | Python 3.13+, pandas, pandas-ta, yfinance       |
| Scheduler  | `schedule` + `zoneinfo` (ET-aware)              |
| Dashboard  | Next.js 15 (Phase 6)                            |
| Container  | Docker Compose (DB only — Python runs locally)  |

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

Kairos follows a **scan → execute → manage** cycle, with CAD as the base portfolio currency.

**Evening:** `update_all()` pulls closing OHLCV → `run_daily_scan()` computes indicators, runs all 5 strategies, and persists signals with strength + z-scores.

**Morning (9:31 AM ET):** `run_morning()` loads last night's signals, fetches the 9:31 AM opening bar for prices and USDCAD FX rate in one batch yfinance call, sizes positions via ATR-based risk, and opens paper trades. Fill prices and FX rates are pinned to the same 9:31 AM bar.

**Evening (5:25 PM ET):** `run_evening()` reads closing prices from the DB (no yfinance call) and checks stop-losses, take-profits, trailing stops, signal reversals, and time-based exits.

### Multi-Currency

| Market | Currency | FX source  |
|--------|----------|------------|
| US     | USD      | USDCAD=X (yfinance, 9:31 AM bar) |
| CA     | CAD      | 1.0 (base) |

FX rates are stored in the `fx_rates` table after the morning fetch; `run_evening()` reuses the same rate rather than re-fetching.

---

## Scheduler (weekdays only, ET)

| Time      | Job                | What it does                              |
|-----------|--------------------|-------------------------------------------|
| 07:00     | Morning digest     | Phase 6 placeholder                       |
| 09:31     | `run_morning()`    | Batch price + FX fetch → execute signals  |
| 17:00     | `update_all()`     | Incremental OHLCV for all tickers         |
| 17:15     | `run_daily_scan()` | Compute indicators → generate signals     |
| 17:25     | `run_evening()`    | DB close prices → stop/TP/exit checks     |
| Every hr  | DB health check    | Runs on weekends too                      |

---

## Manual Commands

```bash
make fetch              # incremental OHLCV update
make scan               # indicators + signal scan
make simulate           # morning execution (9:31 bar)
make simulate-evening   # evening position management
```

---

## Makefile Targets

| Target             | Description                                                    |
|--------------------|----------------------------------------------------------------|
| `install`          | Create `backend/.venv`, install requirements                   |
| `up`               | Start TimescaleDB container                                    |
| `down`             | Stop the container                                             |
| `logs`             | Stream Docker container logs                                   |
| `setup`            | Init schema, seed watchlist, backfill 5yr OHLCV                |
| `run`              | Start the weekday scheduler                                    |
| `fetch`            | One-off incremental OHLCV update                               |
| `scan`             | One-off signal scan                                            |
| `simulate`         | Morning execution (9:31 bar prices + FX)                       |
| `simulate-evening` | Evening management (DB close prices, stop/TP checks)           |
| `shell-db`         | Open `psql` session                                            |
| `reset-db`         | **DESTRUCTIVE** — wipe all data and recreate the database      |

---

## Project Structure

```
kairos/
├── backend/
│   ├── db/
│   │   ├── init.sql            ← schema (auto-run by Docker on first start)
│   │   └── connection.py       ← ONLY file that reads/writes the DB
│   ├── data/
│   │   ├── fetcher.py          ← yfinance downloader with rate limiting
│   │   └── hydrate_fx_rates.py ← backfill USDCAD/CADUSD from 2017
│   ├── strategies/
│   │   ├── scanner.py          ← daily scan orchestrator
│   │   ├── indicators.py       ← technical indicator computation
│   │   ├── regime.py           ← market regime detection (ADX + SPY crisis)
│   │   ├── rsi.py              ← mean reversion
│   │   ├── momentum.py         ← MA crossover + trend following
│   │   ├── macd.py             ← MACD + ADX crossover
│   │   ├── reversal.py         ← short-term reversal (Jegadeesh 1990)
│   │   └── sector_rotation.py  ← ETF-based macro regime rotation
│   ├── simulator/
│   │   ├── simulator.py        ← morning + evening job orchestrator
│   │   ├── executor.py         ← signal → trade, price + FX fetch
│   │   ├── portfolio.py        ← in-memory portfolio with risk limits
│   │   └── position_manager.py ← stop/TP/trailing stop checker
│   ├── scheduler.py            ← long-running weekday job runner
│   ├── setup.py                ← one-time init script
│   └── .env.example
├── dashboard/                  ← Phase 6 (Next.js)
├── docker-compose.yml
├── Makefile
└── README.md
```

---

## Database

**Connection** (after `make up`): `localhost:5432 db=kairos user=kairos pass=kairos_dev`  
Or: `make shell-db`

### Tables

| Table                 | Type       | Description                                   |
|-----------------------|------------|-----------------------------------------------|
| `price_data`          | hypertable | OHLCV bars (daily + intraday)                 |
| `indicators`          | hypertable | Computed technical indicators per ticker/day  |
| `signals`             | regular    | Strategy signals with strength + z_score      |
| `trades`              | regular    | Paper trades with fill price, SL, TP, FX rate |
| `portfolio_snapshots` | hypertable | Point-in-time portfolio state (CAD)           |
| `watchlist`           | regular    | Universe of tracked tickers                   |
| `fx_rates`            | hypertable | Daily USDCAD / CADUSD rates                   |
| `fetch_log`           | regular    | Audit log for every yfinance fetch             |

**Indicators computed:** `rsi_14`, `ma_50`, `ma_200`, `ema_20`, `bb_upper/mid/lower`, `atr_14`, `adx_14`, `volume_sma`, `macd_line`, `macd_signal`, `macd_hist`, `roc_20`

---

## Configuration (.env)

| Variable                | Default   | Notes                                  |
|-------------------------|-----------|----------------------------------------|
| `INITIAL_CAPITAL`       | 100000.0  | Starting cash (portfolio currency)     |
| `PORTFOLIO_CURRENCY`    | CAD       | Base currency for all valuations       |
| `MARKET_OPEN_HOUR_ET`   | 9         | Hour for price/FX cutoff              |
| `MARKET_OPEN_MINUTE_ET` | 31        | Minute for price/FX cutoff            |
| `MAX_PORTFOLIO_RISK`    | 0.02      | Fraction of portfolio risked per trade |
| `ATR_MULTIPLIER`        | 2.0       | Stop-loss distance in ATR units        |
| `TAKE_PROFIT_ATR_MULT`  | 3.0       | Take-profit distance in ATR units      |
| `MAX_POSITION_SIZE`     | 0.10      | Max single position as % of total      |
| `MAX_TOTAL_EXPOSURE`    | 0.80      | Max invested fraction                  |
| `MAX_SECTOR_EXPOSURE`   | 0.30      | Max single sector fraction             |
| `MAX_OPEN_POSITIONS`    | 20        | Hard cap on concurrent positions       |
| `MIN_SIGNAL_STRENGTH`   | 0.10      | Signals below this are skipped         |

---

## Strategies

| Strategy        | Style              | Key Indicators             |
|-----------------|--------------------|----------------------------|
| `rsi`           | Mean reversion     | RSI-14, Bollinger Bands    |
| `momentum`      | Trend following    | MA-50/200 crossover, ADX   |
| `macd`          | Momentum crossover | MACD, ADX                  |
| `reversal`      | Short-term reversal| ROC-20 percentile rank     |
| `sector_rotation` | Macro rotation   | ETF ROC (XLE, XLK, XLU…)  |

All strategies are regime-filtered (TRENDING / CHOPPY / CRISIS) and volume-confirmed. Signals are prioritised by z-score before execution. See [Allocation.md](Allocation.md) for full formulas.

---

## Phase Status

| Phase | Description           | Status      |
|-------|-----------------------|-------------|
| 1     | Data pipeline         | ✅ Complete |
| 2     | Strategy engine       | ✅ Complete |
| 3     | Paper trade simulator | ✅ Complete |
| 4     | Backtesting engine    | ⬜ Pending  |
| 5     | Risk analytics        | ⬜ Pending  |
| 6     | Dashboard (Next.js)   | ⬜ Pending  |
| 5     | AI / sentiment layer   | ⬜ Pending  |
| 6     | Dashboard + notifier   | ⬜ Pending  |

---

## Timezone Contract

All DB timestamps are UTC. The scheduler uses `zoneinfo("America/New_York")` for ET-aware scheduling. `yfinance` returns ET-localised timestamps — `fetcher.py` converts to UTC before every DB write.
