# CLAUDE.md

Guidance for Claude Code (and any AI agent) working in this repository. Read this first; it explains what Kairos is, where the load-bearing pieces live, and the conventions that, if violated, will silently break trading logic.

---

## 1. What this project is

**Kairos** is an algorithmic **paper-trading** system that runs 5 strategies on ~600 US (S&P 500) and Canadian (TSX 60) equities. Everything is self-simulated against a local TimescaleDB — there is no broker account, no real money, and no live order routing. Every trade is logged in plain English along with the indicators and regime that justified it.

The system is structured as a **monorepo** with two halves:

- **`backend/`** — Python 3.13. Data fetcher, indicator computation, strategy engine, simulator, ROOS backtester, FastAPI read-only API, and a long-running scheduler.
- **`dashboard/`** — Next.js 15 (App Router) + React 19 + TanStack Query + Recharts + Tailwind/shadcn. Read-only UI on top of the FastAPI server.

The two halves communicate **only** through the FastAPI HTTP API (`localhost:8000`). The dashboard never touches the database directly.

The day-to-day flow:

1. **17:00 ET** — fetch closing OHLCV for the whole watchlist (`update_all()` in `data/fetcher.py`).
2. **17:15 ET** — compute indicators, run 5 strategies, persist signals with strength + cross-universe z-scores (`strategies/scanner.py`).
3. **09:31 ET next day** — fetch the 9:31 opening bar in one yfinance batch call, fetch USDCAD FX, execute last night's signals (size positions via ATR), open paper trades (`simulator/simulator.py: run_morning`).
4. **10:00–15:58 ET hourly** — intraday position management on live prices: stop-loss, take-profit, trailing stops (`run_intraday`).
5. **17:25 ET** — evening position management from DB close prices (`run_evening`).

CAD is the **portfolio base currency** by default (`PORTFOLIO_CURRENCY=CAD` in `.env`). US positions are converted via the 9:31 ET USDCAD rate stored in `fx_rates`.

---

## 2. Architecture overview

### 2.1 Backend layout (`backend/`)

```
backend/
├── db/
│   ├── init.sql                ← schema (auto-applied by Docker on first container start)
│   └── connection.py           ← THE ONLY FILE that imports psycopg2/sqlalchemy
├── data/
│   ├── fetcher.py              ← yfinance OHLCV (update / backfill / retry-failed)
│   ├── hydrate_fx_rates.py     ← USDCAD backfill from 2017
│   ├── hydrate_indicators.py   ← full historical indicators (needed before backtest)
│   └── fetch_vix.py            ← VIX history backfill from 2017
├── strategies/
│   ├── scanner.py              ← orchestrates the daily scan
│   ├── indicators.py           ← computes indicators for the latest bar
│   ├── regime.py               ← ADX-based TRENDING/CHOPPY/CRISIS detection
│   ├── vol_regime.py           ← VIX regime + VROC spike (Phase 4.5/4.6)
│   ├── rsi.py                  ← mean reversion (RSI + BBands)
│   ├── momentum.py             ← MA-50/200 crossover + continuation
│   ├── macd.py                 ← MACD + ADX
│   ├── reversal.py             ← short-term reversal (Jegadeesh 1990)
│   └── sector_rotation.py      ← ETF-based macro rotation
├── simulator/
│   ├── simulator.py            ← run_morning / run_evening / run_intraday — ONLY file in here that writes to DB
│   ├── executor.py             ← signal → trade, ATR position sizing, live price + FX fetch
│   ├── portfolio.py            ← in-memory Portfolio class (cash, positions, risk limits)
│   └── position_manager.py     ← SL/TP/trailing/reversal/time exit checks
├── backtesting/
│   ├── config.py               ← ROOS params + ~17 named allocation configs
│   ├── data_loader.py          ← bulk DB loaders + ROOS window generator
│   ├── portfolio_runner.py     ← day-by-day simulation engine (largest file, ~1500 lines)
│   └── run_backtest.py         ← CLI entry point (`make backtest`)
├── api/
│   └── api.py                  ← FastAPI app (read-only endpoints)
├── notifier/                   ← placeholder (Phase 6)
├── ai/                         ← placeholder (Phase 6)
├── scheduler.py                ← long-running ET-aware weekday job runner
├── setup.py                    ← one-shot init: seeds watchlist + backfills OHLCV
├── watchlist_extra.csv         ← extra tickers (ETFs, custom) to seed alongside S&P 500 + TSX 60
├── requirements.txt
└── .env.example
```

### 2.2 Dashboard layout (`dashboard/`)

```
dashboard/
├── app/                        ← Next.js App Router pages
│   ├── layout.tsx              ← root layout, sidebar, dark theme, font setup
│   ├── page.tsx                ← / — portfolio dashboard
│   ├── trades/page.tsx         ← /trades
│   ├── backtest/page.tsx       ← /backtest — list of runs
│   ├── backtest/[runId]/page.tsx  ← /backtest/{uuid} — run detail
│   └── globals.css
├── components/
│   ├── app-sidebar.tsx         ← top-level nav (Dashboard, Trades, Backtest)
│   ├── providers.tsx           ← QueryClientProvider + TooltipProvider
│   ├── error-boundary.tsx
│   └── ui/                     ← shadcn primitives (button, card, table, sheet, chart, …)
├── features/                   ← feature-scoped components, grouped by route
│   ├── metrics/                ← VIX / SPY / oil / CAD live tiles
│   ├── portfolio/              ← chart + stats
│   ├── positions/              ← table + drawer (per-ticker chart)
│   ├── trades/                 ← trade history table
│   └── backtest/               ← run list, run detail, charts (equity, drawdown, monthly heatmap)
├── hooks/
├── lib/
│   ├── api/
│   │   ├── client.ts           ← axios instance — base URL from NEXT_PUBLIC_API_URL (default :8000)
│   │   └── queries.ts          ← all useQuery hooks + the queryKeys registry
│   ├── types/api.ts            ← TS types matching backend/api/api.py responses 1:1
│   └── utils.ts
├── public/
├── package.json
├── tsconfig.json               ← `@/*` path alias → `./*`
├── components.json             ← shadcn config (style="radix-mira", baseColor="zinc")
├── next.config.ts
└── eslint.config.mjs
```

### 2.3 Data flow (single page-load on `/`)

```
Browser → GET http://localhost:8000/api/dashboard
                         │
                         ▼
              FastAPI api/api.py
                ├─ get_latest_snapshot()  (DB: portfolio_snapshots)
                ├─ get_portfolio_history(365)
                └─ yf.download([positions + ^GSPC, CL=F, CAD=X, ^VIX], 1d/1m)
                         │
                         ▼  one JSON payload
              dashboard via useDashboard() (TanStack Query, 20s refetch)
                         │
                         ▼
              MetricsCards / PortfolioChart / PortfolioStats / PositionsTable
```

---

## 3. Database

### 3.1 Connection

- **Engine:** TimescaleDB (PostgreSQL 16 + time-series ext).
- **Container name:** `kairos_db` (defined in `docker-compose.yml`).
- **Schema:** `backend/db/init.sql` (auto-applied by Docker the *first* time the volume is created — to re-apply, `make reset-db`).
- **DSN (local dev):** `host=localhost port=5432 db=kairos user=kairos pass=kairos_dev`.
- **Shell access:** `make shell-db` (opens `psql` inside the container).

### 3.2 The single-DB-gateway rule

**Only `backend/db/connection.py` is allowed to import `psycopg2` or `sqlalchemy`.** Every other module imports the named functions from there (`get_watchlist`, `insert_signals`, `get_latest_indicators`, `save_portfolio_snapshot`, …). This is enforced socially, not by tooling — please don't break it.

Inside `connection.py`:
- `get_engine()` returns a process-cached SQLAlchemy engine with `pool_pre_ping=True`.
- `get_conn()` is a context manager wrapping `psycopg2` — **commits on clean exit, rolls back on any exception**.
- `to_utc(dt)` rejects naive datetimes — see §6.1.

### 3.3 Tables (see `backend/db/init.sql` for full DDL)

| Table | Type | Purpose |
|---|---|---|
| `price_data` | hypertable | OHLCV bars; `UNIQUE (time, ticker, interval)` so re-fetch is idempotent |
| `indicators` | hypertable | One row per (time, ticker) with RSI, MA, BB, ATR, ADX, MACD, ROC, volume_sma |
| `signals` | regular | Strategy signals — strength, z_score, regime, reason, `acted_on` flag |
| `trades` | regular | Paper trades — fill_price, SL, TP, strategy, reason, `status` ∈ {filled,closed}, `fill_type` |
| `portfolio_snapshots` | hypertable | Point-in-time portfolio state; positions JSONB; currency-tagged |
| `watchlist` | regular | Universe — `ticker, name, sector, market(US/CA), active, notes` |
| `fx_rates` | hypertable | `USDCAD` rates at 9:31 ET (one row per trading day per pair) |
| `vix_data` | hypertable | Daily VIX close (Phase 4.5+ vol regime) |
| `fetch_log` | regular | Audit row per fetch run |
| `backtest_results` | regular | One row per (config × window) — Sharpe/Calmar/CAGR/MaxDD + full config JSONB + run_id UUID grouping each `make backtest` execution |
| `backtest_analytics` | regular | Pre-computed chart payloads per backtest row (equity curve, drawdown, monthly heatmap, daily snapshots) |

`run_id` (UUID) groups all (config × window) rows produced by a single invocation of `make backtest`. The dashboard's `/backtest` page is one row per `run_id`.

---

## 4. Running the system

### 4.1 First-time setup

```bash
make install            # creates backend/.venv and installs requirements
make up                 # docker compose up -d  (starts kairos_db)
make setup              # seeds watchlist (~563 tickers from Wikipedia + watchlist_extra.csv)
                        # then backfills ~10y of daily OHLCV
make hydrate-fx         # backfills USDCAD from 2017 (needed for backtests + CAD positions)
make hydrate-indicators # computes indicators for every historical bar (REQUIRED before backtest)
make fetch-vix          # backfills VIX history from 2017 (REQUIRED for vol_filter / soft CB configs)
```

`make setup` enforces that you're inside `backend/.venv` (it `sys.exit(1)`s otherwise). All Make targets that touch the DB are gated by `_guard-docker` / `_guard-venv`, so they fail fast with a useful error.

### 4.2 Live operation

```bash
make run                # caffeinate + scheduler.py — keeps weekday jobs firing in ET
```

The scheduler polls every 30 s, dispatches ET-timed jobs at `(hour, minute)` slots, and **dedupes by `(date, hour, minute)`** so it doesn't double-fire when the poll loop straddles a minute boundary. It also honors NYSE and TSX holiday calendars (computed in `_nyse_holidays` / `_tsx_holidays`); intraday management runs if either market is open.

### 4.3 Manual / one-off commands

```bash
make fetch                       # incremental OHLCV update for all active tickers
make fetch-debug                 # same with LOGURU_LEVEL=DEBUG
make retry-failed DATE=YYYY-MM-DD # re-run failed tickers from fetch_log
make scan                        # signal scan once (DATE=… for historical replay)
make simulate                    # morning execution once (DATE=… for replay)
make simulate-evening            # evening management once
make api                         # FastAPI on :8000 (--reload)
make api-debug                   # same with LOGURU_LEVEL=DEBUG
make dashboard                   # cd dashboard && pnpm install && pnpm dev (port 3000)
make shell-db                    # psql session
make reset-db                    # DESTRUCTIVE — wipes volume + re-applies init.sql (asks for "yes")
```

`make scan`, `make simulate`, and `make simulate-evening` accept `DATE=YYYY-MM-DD` for **historical replay** — they read DB prices/indicators/FX for that date instead of calling yfinance. Use this to catch up after missed days without polluting your real-time state.

### 4.4 Backtesting (`make backtest`)

```bash
make backtest                                   # all configs × both capital modes
make backtest-config CONFIG=regime_adaptive     # one config × both capital modes
make backtest-config-all-modes CONFIG=foo       # explicit
make backtest-mode CAPITAL_MODE=capital_refresh # all configs × one mode
# Optional knobs:  WORKERS=N  CAPITAL_MODE=capital_refresh|capital_compounded|all
```

All backtest invocations are wrapped in `caffeinate -i` so a long run survives macOS sleep. Each run inserts a fresh `run_id` (UUID) per (config, capital_mode), with one `backtest_results` row per window plus one `backtest_analytics` row per result.

Capital modes:
- **`capital_refresh`** — each ROOS window restarts at `INITIAL_CAPITAL` (independent samples).
- **`capital_compounded`** — windows roll forward; ending capital of window N is starting capital of window N+1.

### 4.5 Dashboard

```bash
cd dashboard && pnpm install && pnpm dev    # or: make dashboard
```

Defaults to `http://localhost:3000`. Override the API host with `NEXT_PUBLIC_API_URL=http://...` at build time. The API is **read-only** — the dashboard cannot mutate trades, signals, or backtests.

### 4.6 Testing

There is **no automated test suite**. There is no `pytest` setup, no jest/vitest. Verification is currently done by:
- Running `make scan` / `make simulate` against real DB data and reading the printed summaries.
- Running a single-config backtest (`make backtest-config CONFIG=…`) and checking the rich-formatted progress + `backtest_results` rows.
- Manually exercising the dashboard against a populated DB.

If you add tests, do it under `backend/tests/` with pytest and let the user know — there is no existing convention to match.

---

## 5. The strategy & simulation pipeline

### 5.1 Daily scan (`strategies/scanner.py: run_daily_scan`)

1. `compute_all()` — `indicators.py` recomputes only the latest row for tickers whose row isn't yet in DB today (incremental). It needs ≥ 200 bars; otherwise the ticker is skipped with a warning.
2. Fetch `SPY` for last 10 bars → `detect_crisis` (down >5% over 5 trading days).
3. `regime.detect_all(today_ind, spy_df)` → per-ticker `{regime: TRENDING|CHOPPY|CRISIS, confidence: float}`. ADX-based thresholds with linear-interpolated confidence (see file docstring).
4. Open positions (`get_open_position_tickers()`) → duplicate guard for BUYs.
5. Pull previous indicator row per ticker (`get_prev_indicators`) — needed for crossover strategies (`momentum`, `macd`).
6. Compute ROC rankings + macro sector scores (`late_cycle`, `defensive`).
7. Call `rsi`, `momentum`, `macd`, `reversal`, `sector_rotation` `.generate_signals(...)` — these are **pure stateless functions**; they receive all data as args and never touch the DB.
8. Compute **cross-universe z-scores per strategy group** (population std, Winsorized at ±3σ).
9. `insert_signals(signals, signal_time=last_bar_time)`. Signal time is anchored to the *price bar* timestamp, not wall-clock — that's how `run_morning` finds them the next day.

Strategy strength formula (common pattern):
```
final_strength = clamp(base * regime_mult * regime_confidence * volume_mult, 0, 1)
```
- `volume_mult` = `clamp(volume / volume_sma, 0.5, 1.5)`, defaulting to 1.0 if `volume_sma` is missing.
- Each strategy has its own `_REGIME_MULT` table — see file docstrings; e.g. RSI is strongest in CHOPPY, momentum strongest in TRENDING, all strategies dampened in CRISIS.

### 5.2 Morning execution (`simulator/simulator.py: run_morning`)

This is the **only** function in `simulator/` that opens new trades.

1. Load portfolio (`Portfolio.load_from_db()`).
2. Walk back to the previous weekday and load that day's signals (signals are written at 17:15 ET ≈ 21–22 UTC, so at 09:31 ET the next morning they sit in *yesterday's UTC* window).
3. **Batch yfinance call** for all signal + position tickers (1-day 1-minute bars). Pick the last bar ≤ `9:31 ET` cutoff. This pins fills to the opening bar so `make simulate` (run later in the day) and the scheduled job produce *identical* fills.
4. Resolve FX: for every non-base currency needed, check `fx_rates` table for a rate ≥ `today_start_utc`. If missing, fetch live → store. Reuse for evening run.
5. **Pre-open exits first** — call `position_manager.check_positions(...)` to fire SL/TP/trailing/reversal/time exits on existing positions *before* opening new trades. Freed cash is immediately available.
6. **Sort signals by z_score DESC** (falling back to strength). High-conviction signals get capital first.
7. For each BUY signal, in `executor.execute_signals`:
   - `qty = floor(strength * MAX_PORTFOLIO_RISK * total_value / risk_per_share_base)` where `risk_per_share = ATR_MULTIPLIER × atr_14`.
   - **Cap-and-fill** instead of rejecting: if `cost > MAX_POSITION_SIZE`, shrink `qty` (`fill_type = "Capped to Max Size"`); if total exposure cap would be breached, do a partial fill (`fill_type = "Partial Fill"` or `"Capped & Partial"`).
   - `take_profit = price + TAKE_PROFIT_ATR_MULT * atr_14`.
   - Run `portfolio.can_open_position` for the final sanity check (max positions, sector cap, cash).
8. Persist via `insert_trade` (one row per fill), `mark_signal_acted_on(signal_id)`.
9. Take a `portfolio.snapshot(current_prices, fx_rates, market_map)` and print the human summary.

### 5.3 Evening management (`run_evening`)

Reads close prices **from `price_data` DB only — no yfinance call** (the 17:00 fetch already wrote today's close). Same `position_manager.check_positions` logic as the morning pre-open phase, but operates on close prices. No new trades opened.

### 5.4 Intraday management (`run_intraday`)

Hourly 10:00–15:58 ET. Fetches live 1-min bars for open positions, fills holiday gaps with `get_latest_close_prices`, runs the same position_manager, persists exits + trailing-stop updates, and *always* takes a snapshot so the equity curve has hourly resolution.

### 5.5 Exit priority in `position_manager.check_positions`

For each open position, **first match wins**:

1. **Stop loss** — `price <= stop_loss` (exit at `price`).
2. **Take profit** — `price >= take_profit`.
3. **Trailing stop** — only if in profit > 5%: `new_stop = price - ATR_MULTIPLIER * atr_14`; if `new_stop > stop_loss`, update (this **mutates** the in-memory position; `simulator.py` then calls `update_stop_loss` in DB).
4. **Signal reversal** — RSI sell with strength > 0.5 against an `rsi`-strategy position, or a `ma_50 < ma_200` death cross against a `momentum` position.
5. **Time exit** — > 30 days open *and* P&L < 0.

### 5.6 Multi-currency

- `watchlist.market` ∈ {`US`, `CA`} → native currency via `_MARKET_CURRENCY = {US: USD, CA: CAD}`.
- Portfolio base currency = `PORTFOLIO_CURRENCY` env (default `CAD`).
- All position values (`cash`, `total_value`, `cost_base`) are in **base currency**. Each position stores its native `currency` + `fx_rate` at open.
- Backtester does the inverse: DB stores `USDCAD` (1 USD = X CAD); `data_loader._fx_pair_info()` inverts it to `CADUSD` so the backtester multiplies native CAD prices by the rate to get USD.

---

## 6. Conventions you must follow

### 6.1 Timezones — *the* most common source of silent bugs

- **Every DB timestamp is UTC.** Always.
- **Naive datetimes are rejected** — `db/connection.to_utc()` raises `ValueError` on `dt.tzinfo is None`. Use `datetime.now(timezone.utc)`, never `datetime.now()`.
- `yfinance` returns ET-localised timestamps for US equities. `data/fetcher._normalise` converts to UTC (`tz_localize("America/New_York").tz_convert("UTC")` if naive, else `tz_convert("UTC")`).
- The scheduler uses `ZoneInfo("America/New_York")` for ET-aware dispatch; DST is handled automatically.
- Trade rows are stamped at `9:31 AM ET` (converted to UTC) via `executor._open_cutoff_utc()` so manual + scheduled runs produce identical timestamps.
- Signal rows are stamped at the price-bar time (`signal_time=trading_day`), not wall-clock — `run_morning` walks back to "previous weekday" to find them.

### 6.2 DB write boundaries

- Only `simulator/simulator.py` calls DB write functions inside the simulator package. `executor.py`, `portfolio.py`, and `position_manager.py` are pure — they receive data, mutate the in-memory `Portfolio`, and return action dicts for `simulator.py` to persist.
- `db/connection.py` is the only file allowed to `import psycopg2` or `sqlalchemy`. Don't bypass it.
- `executor.py` never queries the DB — `sector_map` and `market_map` are pre-fetched and passed in.

### 6.3 Money rounding

- Fill prices, stop losses, take profits → 2 decimal places at DB insert (`round(float(x), 2)` in `insert_trade`).
- FX rates → 6 decimal places (`insert_fx_rate`).
- Always-base-currency: `cash`, `total_value`, P&L, exposure caps. Never mix native and base.

### 6.4 ATR-based sizing

- Position size depends on `atr_14`. If `atr_14` is missing or zero, the trade is **skipped** (not estimated). `indicators.py` will reject the whole indicator row if any of `{rsi_14, ma_50, ma_200, atr_14, adx_14}` is NaN — `volume_sma` is allowed to be NaN (defaults to mult=1.0).

### 6.5 Strategy functions are pure

Every file in `strategies/` (except `scanner.py`) exposes a `generate_signals(...)` that:
- takes pre-fetched data dicts (indicator rows, regimes, open position tickers, etc.),
- returns a list of signal dicts shaped for `insert_signals()`,
- has zero side effects, zero DB calls, zero yfinance calls.

If you add a new strategy, follow that contract and wire it into `scanner.py` + the `_strategies` list. The backtester picks up live strategies automatically because `portfolio_runner.py` calls the same functions — see §6.7.

### 6.6 Signals always carry their reason

Every signal dict has a human-readable `reason` field that gets persisted to `signals.reason` and `trades.reason`. Don't drop it — the dashboard / trade history rely on it for explainability.

### 6.7 Backtester ≡ live engine

The ROOS engine (`backtesting/portfolio_runner.py`) **calls the live strategy functions directly** — there is no reimplementation. If a strategy changes, backtest behavior changes too. This is intentional: it eliminates the most common source of backtest/live divergence.

Consequences:
- **Don't fork strategy logic** between live and backtest. If you need backtest-only behavior, put it in `portfolio_runner.py` or in config flags consumed by both sides.
- **Fill at Open, value at Close** — matches `run_morning` exactly. No look-ahead bias.
- **Data loaded once per config** (full date range), then sliced per window. Be careful when adding new data sources — load them upfront in `load_*` and pass via the `preloaded` dict for parallel workers.

### 6.8 Allocation configs

`backtesting/config.CONFIGS` is the source of truth for named configs. To add one, copy an existing dict, give it a unique key, and re-run `make backtest`. The schema is permissive — every consumer in `portfolio_runner.py` uses `config.get(key, default)`. See `BACKTEST_ALLOCATION.md` and `ROOS_EXPLAINER.md` for the math and design rationale.

### 6.9 Dashboard conventions

- **Path alias:** `@/*` → `./*` (e.g. `@/lib/api/queries`). Use it, don't write relative paths.
- **shadcn:** primitives in `components/ui/`. Style preset `"radix-mira"`, base color `"zinc"`. New primitives should be added with `npx shadcn add ...` so they stay consistent.
- **Feature grouping:** components specific to a route live under `features/<area>/...`. Cross-cutting bits go in `components/`.
- **Data fetching:** every page reads via TanStack Query hooks in `lib/api/queries.ts`. Each query has a key in `queryKeys` — register new ones there, don't inline.
- **Refetch policy:**
  - `useDashboard()` — 20s stale + 20s refetchInterval (live trading view).
  - `useTrades()` — 60s stale, 5min refetch.
  - `useBacktest*()` — 5min stale, manual invalidation.
- **Skeletons:** every data-loading component renders its own skeleton (`*-skeleton.tsx`) so server + client HTML match.
- **Types:** `lib/types/api.ts` mirrors `backend/api/api.py` responses 1:1. If you change a response shape, update both — there is no codegen.
- **Theme:** dark-only (`<html className="dark">`). Don't add a theme toggle without buy-in.
- **Package manager:** `pnpm`. `pnpm-workspace.yaml` exists but currently only the dashboard uses it.

### 6.10 Logging

- **Backend:** `loguru`. Toggle verbosity with `LOGURU_LEVEL=DEBUG`. The backtest entry point (`backtesting/run_backtest.py`) silences loguru entirely and uses `rich` progress + `print()` for the operator-facing output. Don't add `print()` in scanner/simulator code — use `logger.info`.

### 6.11 What NOT to do

- Don't import `psycopg2`/`sqlalchemy` outside `db/connection.py`.
- Don't add `datetime.now()` without `tz=timezone.utc`.
- Don't fetch FX from yfinance inside a tight loop — fetch once in `run_morning` / `run_evening`, persist to `fx_rates`, reuse.
- Don't introduce a new DB write function in `simulator/portfolio.py`, `simulator/executor.py`, or `simulator/position_manager.py`. They're deliberately pure — `simulator.py` is the only place that persists.
- Don't compute `z_score` per-strategy in any file other than `scanner.py` and `portfolio_runner.py` (which both use the same Winsorized formula). If you change one, change the other.
- Don't run `make reset-db` casually — it nukes the Docker volume *and* requires re-running `make setup` + `make hydrate-indicators` + `make hydrate-fx` + `make fetch-vix` (cumulatively ~30+ minutes of yfinance traffic).
- Don't commit `.env`, `backend/.venv/`, `node_modules/`, `.next/` — already gitignored.

---

## 7. Configuration

`backend/.env` (copied from `backend/.env.example` by `make setup`):

| Var | Default | Purpose |
|---|---|---|
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `localhost` / `5432` / `kairos` / `kairos` / `kairos_dev` | Postgres connection |
| `INITIAL_CAPITAL` | `100000.0` | Starting cash (portfolio base currency) |
| `PORTFOLIO_CURRENCY` | `CAD` | Base currency |
| `MARKET_OPEN_HOUR_ET` / `MARKET_OPEN_MINUTE_ET` | `9` / `31` | Opening-bar cutoff for fills + FX |
| `MAX_PORTFOLIO_RISK` | `0.02` | Fraction of portfolio risked per trade (strength-weighted) |
| `ATR_MULTIPLIER` | `2.0` | Stop-loss distance in ATR units |
| `TAKE_PROFIT_ATR_MULT` | `3.0` | Take-profit distance in ATR units |
| `MAX_POSITION_SIZE` | `0.10` | Max single position as % of total value |
| `MAX_TOTAL_EXPOSURE` | `0.80` | Max invested fraction |
| `MAX_SECTOR_EXPOSURE` | `0.30` | Max single sector fraction |
| `MAX_OPEN_POSITIONS` | `20` | Hard cap on concurrent positions |
| `MIN_SIGNAL_STRENGTH` | `0.10` | Signals below this are skipped at execution |
| `HISTORICAL_YEARS` | `11` | Setup backfill horizon |
| `LOGURU_LEVEL` | (unset) | Set to `DEBUG` for verbose backend logs |
| `NEXT_PUBLIC_API_URL` (dashboard only) | `http://localhost:8000` | API base for the Next.js client |

The backtester reads the same env vars (via `backtesting/config.py`) so live + backtest stay in sync.

---

## 8. FastAPI surface

The API is **read-only** and CORS-open (`allow_origins=["*"]`, `allow_methods=["GET"]`). All endpoints log timing via a middleware. Sources of truth are in `backend/api/api.py`:

| Endpoint | Description |
|---|---|
| `GET /api/dashboard` | Portfolio snapshot + market metrics (^GSPC, CL=F, CAD=X, ^VIX) — single yfinance batch call |
| `GET /api/trades` | All trades (up to 10k), newest first |
| `GET /api/backtest?config=…&run_id=…` | `{runs, summary, run_list}` — used by the backtest detail page |
| `GET /api/backtest/runs` | One row per `run_id` with aggregated metrics |
| `GET /api/backtest/analytics?run_id=…` | Pre-computed chart payloads (equity curve, drawdown, monthly heatmap, daily snapshots) |
| `GET /api/ticker/{ticker}?range=1D\|1W\|1M\|3M\|6M\|YTD\|1Y\|5Y` | OHLCV + per-ticker trade overlay for the position drawer. `1D` = live yfinance 1-min bars; everything else = DB |

The ticker route validates the symbol against `^[A-Z0-9.\-=^]{1,20}$` and rejects unknown ranges. A `threading.Lock` (`_yf_lock`) serialises yfinance calls across concurrent requests.

Docs: `http://localhost:8000/api/docs`.

---

## 9. Project status (current phase)

From `README.md`:

| Phase | Description | Status |
|---|---|---|
| 1–3 | Data pipeline, strategy engine, paper trade simulator | ✅ |
| 4 | ROOS backtesting engine | ✅ |
| 4.5 | VIX vol regime filter | ✅ |
| 4.6 | VROC spike trigger | ✅ |
| 4.7 | Drawdown circuit breakers (hard) | ✅ |
| 4.8 | Soft CB + crisis limits + dollar-risk floor | ✅ |
| 4.10 | Multi-trigger recovery + dynamic floor | ✅ |
| 5 | Risk analytics (live side) | ⬜ |
| 6 | AI / sentiment / morning digest | ⬜ |
| 7 | Dashboard + notifier | 🔧 in progress |

Current branch: `feat/backtesting`. Most recent commits center on backtesting polish (charts, deadlocks, VIX trigger, circuit breakers).

For deeper context on the trading logic, read `ALLOCATION.md` (live signal sizing) and `BACKTEST_ALLOCATION.md` + `ROOS_EXPLAINER.md` (ROOS engine math).

---

## 10. When in doubt

- The `README.md` has the full operator-facing manual including a mermaid diagram of the ROOS pipeline.
- `ALLOCATION.md`, `BACKTEST_ALLOCATION.md`, `ROOS_EXPLAINER.md` document the math behind signal strength, z-scores, and the rolling-out-of-sample protocol.
- Strategy files are heavily docstring'd — the formula and regime multipliers are at the top of each.
- `db/connection.py` function docstrings cover the timestamp / timezone contract.
- For "why does the scheduler fire at this exact time?", read the `_ET_JOBS` table at the bottom of `scheduler.py`.
- For "where do trades come from?", trace `scheduler._job_morning_execution → simulator.run_morning → executor.execute_signals → db.insert_trade`.

If you're about to make a change that crosses any of these layers — strategy → simulator → DB schema, or backend response → dashboard type — touch all the relevant files in the same change, since there's no codegen catching drift.
