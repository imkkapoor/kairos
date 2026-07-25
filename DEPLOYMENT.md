# DEPLOYMENT.md

How to take Kairos from the local `make run` setup to a small always-on cloud deployment.

This documents the target architecture, the trade-offs behind it, and a concrete
build checklist. It reflects the actual state of the repo: the Python side runs in
a venv (there is **no** backend Dockerfile), and `docker-compose.yml` defines only
TimescaleDB — which is not used in prod once the DB moves to a managed Postgres.

---

## 1. Target architecture

Three pieces, only one of which is infrastructure you actually build:

```
Vercel      → dashboard/  (Next.js)      managed — ~zero infra to build
Supabase    → Postgres                   managed — ~zero infra to build
Lightsail   → scheduler.py + FastAPI     ← the one box you provision
```

- **Vercel** hosts the Next.js dashboard. Vercel builds Next.js, so this is a
  connect-repo-and-deploy step, not infra.
- **Supabase** is managed Postgres. Kairos does not depend on TimescaleDB
  hypertable features at this scale, so plain Postgres is fine. (Supabase, like
  RDS, does not offer the `timescaledb` extension — hypertables degrade to
  regular tables, which is acceptable here.)
- **Lightsail** (a small Linux VM) runs the two always-on Python processes.
  This is the only "infra" in the real sense.

### Why a box at all (and not fully serverless)

Vercel is serverless — it cannot keep a long-running process alive. Kairos's
Python side has two always-on components:

1. **`scheduler.py`** — fires the weekday ET jobs (fetch / scan / morning
   execution / intraday / evening). Must run 24/5.
2. **`api.py` (FastAPI)** — the read-only HTTP API the dashboard calls.

Both are tiny and both fit on one small box. That box exists purely because
something has to hold the scheduler loop; Vercel and Supabase can't.

---

## 2. Trimming the prod database

Prod does **not** need the full historical backfill or the backtest tables.

- **Drop entirely:** `backtest_results`, `backtest_analytics`, and the bulk of
  historical OHLCV (prod never writes backtest data).
- **Keep:** ~1–2 years of daily OHLCV for the ~600 tickers, matching
  `indicators`, plus `fx_rates`, `vix_data`, `watchlist`, and the live tables
  (`signals`, `trades`, `portfolio_snapshots`).

**The binding constraint:** indicators need **≥200 daily bars per ticker**
(MA-200 — `indicators.py` skips any ticker with fewer). So you cannot go tiny;
~1 year (≈252 trading days) is the practical floor, ~2 years is comfortable.

Trimmed size lands around 300–500 MB, which matters for Supabase's free tier
(500 MB). ~1 year likely fits free; ~2 years may push you to Supabase Pro
($25/mo, 8 GB).

---

## 3. What you build on the Lightsail box

Recommended instance: **2 GB Lightsail (Ubuntu)**. 1 GB can be tight during the
morning yfinance batch (600 tickers of 1-minute bars through pandas).

1. **Runtime** — Python 3.13 + `backend/.venv` via `make install`. No containers
   needed.
2. **Two systemd services** (replacing the macOS `caffeinate` in `make run`):
   - `kairos-scheduler` → `python backend/scheduler.py`
   - `kairos-api` → `uvicorn`/`gunicorn` serving FastAPI on `:8000`
   - `Restart=always` on both so they survive crashes and reboots.
   - The scheduler is timezone-safe (`ZoneInfo("America/New_York")`), so the
     box's clock/region does not matter.
3. **Reverse proxy with TLS — the non-obvious requirement.** The dashboard calls
   the API **from the browser** (`NEXT_PUBLIC_API_URL` → a client-side axios
   instance). Vercel serves over HTTPS, so browsers will **block plain-HTTP calls
   to the API** (mixed content). The API therefore must be HTTPS:
   - a domain/subdomain (e.g. `api.yourdomain.com`) → the box's static IP
   - **Caddy** (easiest) or nginx+certbot in front of uvicorn, auto-provisioning
     a Let's Encrypt cert, reverse-proxying `443 → localhost:8000`
   - CORS is already open in `api.py` (`allow_origins=["*"]`), so no code change.
4. **Firewall** — allow 443 (API), 80 (cert issuance), 22 (SSH). Nothing else.
5. **Config** — a locked-down `backend/.env` (`chmod 600`) pointing at Supabase's
   pooler endpoint. On a single box this is sufficient; AWS Secrets Manager is
   not required.

---

## 4. Supabase setup (configuration, not infra)

1. Create the project.
2. Apply the schema: run `backend/db/init.sql` against it.
3. Load the trimmed dataset — export ~1–2 yr of `price_data` / `indicators` +
   `fx_rates` / `vix_data` / `watchlist` from local `kairos_db`, import to
   Supabase. Skip the backtest tables.
4. Use the **transaction-pooler** connection string (port `6543`) in `.env`.
   `connection.py`'s `pool_pre_ping=True` already handles Supabase closing idle
   connections.
5. Free tier = 500 MB (fits ~1 yr; ~2 yr may need Pro).
6. Kairos hits the DB daily, so the free-tier idle-pause won't trigger.

---

## 5. Vercel setup (configuration, not infra)

1. Connect the repo; set the project root to `dashboard/` (Next.js auto-detected).
2. Set `NEXT_PUBLIC_API_URL=https://api.yourdomain.com`.
3. Deploy.

---

## 6. Build checklist

1. Provision Lightsail 2 GB instance (Ubuntu) + attach a static IP.
2. Point a domain/subdomain at that IP.
3. On the box: install Python 3.13, clone the repo, `make install`.
4. Create the Supabase project → apply `init.sql` → load trimmed data → note the
   pooler DSN.
5. Write `backend/.env` (Supabase DSN + trading config vars).
6. Write the two systemd units (scheduler + api).
7. Install Caddy → reverse-proxy `443 → localhost:8000`, auto-TLS.
8. Open firewall 443 / 80 / 22.
9. Deploy the dashboard on Vercel with `NEXT_PUBLIC_API_URL` set.
10. Verify: dashboard loads live data over HTTPS; scheduler logs show jobs firing
    at the correct ET times.

**First-run ordering (to trade the next morning):** the 09:31 ET morning
execution consumes signals written by the prior evening's 17:15 scan. Make sure a
scan has run (scheduled or `make scan`) before the first morning execution, or it
will open nothing.

---

## 7. Rough monthly cost

| Piece | Tier | ~Cost |
|---|---|---|
| Lightsail | 2 GB, static IP + bandwidth bundled | ~$12/mo |
| Supabase | Free (≈1 yr history) or Pro (≈2 yr, 8 GB) | $0 or $25/mo |
| Vercel | Hobby | $0 |

**Total: ~$12–37/mo.** The only genuinely "built" infra is the Lightsail box with
its two systemd services + Caddy; the rest is managed platforms pointed at each
other.

---

## 8. Alternatives considered

- **Lightsail vs EC2:** Lightsail runs on EC2 under the hood but bundles
  compute + storage + bandwidth + static IP at a flat, predictable price with a
  simpler console. EC2 is à-la-carte (separate compute / EBS / egress / ~$3.65/mo
  public IPv4) with full control and deep AWS integration. For one small always-on
  box, **Lightsail** is the better fit; reach for EC2 only if you later need
  autoscaling or tight AWS-service integration.
- **ECS Fargate:** run containers without a VM (~$9/mo) — more setup (task defs,
  cluster) for little gain solo.
- **Lambda + EventBridge + API Gateway:** near-$0 compute, but a refactor — the
  `scheduler.py` poll loop would be replaced by EventBridge schedules mapping each
  `_ET_JOBS` slot to a Lambda. Only worth it if already deep in AWS.
- **Single-box with DB in a container:** run Postgres/Timescale in a container on
  the same box instead of Supabase — keeps real TimescaleDB and is cheapest
  (~$16/mo all-in), but you own backups/patching.

### The subtlety that bites people

Item 3 in §3 — the browser-to-API HTTPS requirement. Because the dashboard calls
the API client-side and Vercel is HTTPS, the API cannot be plain HTTP. A domain +
Caddy auto-TLS solves it; skipping it means the dashboard silently fails to load
live data in production.
