# Kairos Dashboard

Next.js 15 frontend for the Kairos algorithmic paper trading system.

## Dev server

```bash
make dashboard          # from repo root (runs pnpm install + pnpm dev)
# or directly:
cd dashboard && pnpm dev
```

Opens at **http://localhost:3000**. Requires the FastAPI backend (`make api`) and TimescaleDB (`make up`) to be running.

---

## Pages

| Route | Description |
|-------|-------------|
| `/` | Live portfolio — equity value, open positions, PnL stats |
| `/trades` | Trade log with entry/exit details, strategy, fill price |
| `/backtest` | ROOS backtest run list — all configs and windows |
| `/backtest/[runId]` | Backtest run detail — equity curve, per-window metrics, vol filter stats |

---

## Backend API

All data comes from the FastAPI server at `http://localhost:8000`.

```
GET /api/dashboard                     Portfolio snapshot + live market metrics (^GSPC, CL=F, CAD=X, ^VIX)
GET /api/trades                        Trade history (up to 10k, newest first)
GET /api/backtest?config=&run_id=      ROOS results { runs, summary, run_list }
GET /api/backtest/runs                 One row per run_id
GET /api/backtest/analytics?run_id=    Pre-computed chart payloads (equity/drawdown/heatmap)
GET /api/backtest/vix?run_id=          VIX series aligned to a backtest window
GET /api/ticker/{ticker}?range=        OHLCV bars + per-ticker trade overlay
```

See `backend/api/api.py` for full route definitions.

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Framework | Next.js 15 (App Router) |
| Styling | Tailwind CSS |
| Components | shadcn/ui |
| Charts | Recharts |
| Data fetching | TanStack Query (React Query) |
| Package manager | pnpm |

---

## Project structure

```
dashboard/
├── app/
│   ├── page.tsx                ← Portfolio overview
│   ├── trades/page.tsx         ← Trade log
│   └── backtest/
│       ├── page.tsx            ← Backtest run list
│       └── [runId]/page.tsx    ← Run detail
├── components/
│   ├── app-sidebar.tsx
│   ├── providers.tsx
│   └── ui/                     ← shadcn/ui primitives
├── features/
│   ├── backtest/               ← Backtest list + detail views
│   ├── metrics/                ← Portfolio metric cards
│   ├── portfolio/              ← Equity chart + stats
│   ├── positions/              ← Open positions table + drawer
│   └── trades/                 ← Trade history table
├── lib/
│   ├── api/
│   │   ├── client.ts           ← Axios instance
│   │   └── queries.ts          ← TanStack Query hooks
│   └── types/api.ts            ← Shared API response types
└── hooks/
    └── use-mobile.ts
```

