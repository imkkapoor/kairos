# =============================================================================
# Kairos — Makefile
# =============================================================================

VENV      := backend/.venv
PYTHON    := $(VENV)/bin/python
UVICORN   := $(VENV)/bin/uvicorn
PIP       := $(VENV)/bin/pip
BACKTEST  := caffeinate -i bash -c 'cd backend && $(abspath $(PYTHON)) -m backtesting.run_backtest

.PHONY: install up down logs setup run scan fetch fetch-debug retry-failed \
        simulate simulate-evening \
        api api-debug dashboard \
        backtest backtest-config backtest-config-all-modes backtest-mode \
        hydrate-indicators hydrate-fx fetch-vix \
        shell-db reset-db help \
        _guard-docker _guard-venv


# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------

## install: Create .venv, upgrade pip, install all requirements
install:
	@if [ ! -d "$(VENV)" ]; then \
		echo "Creating virtual environment at $(VENV)..."; \
		python3 -m venv $(VENV); \
	fi
	$(PIP) install --upgrade pip
	$(PIP) install -r backend/requirements.txt
	@echo ""
	@echo "Done. Run: make up → make setup → make run"


# -----------------------------------------------------------------------------
# Docker / Database
# -----------------------------------------------------------------------------

## up: Start the TimescaleDB container
up:
	docker compose up -d

## down: Stop the TimescaleDB container
down:
	docker compose down

## logs: Stream Docker logs (Ctrl+C to stop)
logs:
	docker compose logs -f

## shell-db: Open a psql session in kairos_db
shell-db:
	docker exec -it kairos_db psql -U kairos -d kairos

## reset-db: DESTRUCTIVE — wipe all data and volumes, recreate from scratch
reset-db:
	@read -p "WARNING: This will DELETE all data and volumes. Type 'yes' to confirm: " _confirm; \
	if [ "$$_confirm" = "yes" ]; then \
		docker compose down -v; \
		docker compose up -d; \
		echo "Database reset complete."; \
	else \
		echo "Aborted — no changes made."; \
	fi


# -----------------------------------------------------------------------------
# Live system
# -----------------------------------------------------------------------------

## setup: Init schema, seed watchlist (S&P500 + TSX60 + watchlist_extra.csv), backfill OHLCV
setup: _guard-docker _guard-venv
	$(PYTHON) backend/setup.py

## run: Start the weekday scheduler (fetch + scan + simulate)
run: _guard-docker _guard-venv
	caffeinate -i $(PYTHON) backend/scheduler.py

## scan: Run the daily signal scan once  (DATE=YYYY-MM-DD for historical replay)
scan: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m strategies.scanner $(if $(DATE),--date $(DATE),)

## simulate: Run morning order execution  (DATE=YYYY-MM-DD for historical replay)
simulate: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m simulator.simulator morning $(if $(DATE),--date $(DATE),)

## simulate-evening: Run evening position management  (DATE=YYYY-MM-DD for historical replay)
simulate-evening: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m simulator.simulator evening $(if $(DATE),--date $(DATE),)

## api: Start the FastAPI server on port 8000 (auto-reload on file changes)
api: _guard-docker _guard-venv
	cd backend && $(abspath $(UVICORN)) api.api:app --host 0.0.0.0 --port 8000 --reload --reload-dir .

## api-debug: Same as api with DEBUG logging (verbose yfinance + DB output)
api-debug: _guard-docker _guard-venv
	cd backend && LOGURU_LEVEL=DEBUG $(abspath $(UVICORN)) api.api:app --host 0.0.0.0 --port 8000 --reload --reload-dir .

## dashboard: Start the Next.js dev server on port 3000
dashboard:
	cd dashboard && pnpm install && pnpm dev


# -----------------------------------------------------------------------------
# Data fetching
# -----------------------------------------------------------------------------

## fetch: Incremental OHLCV update for all active tickers
fetch: _guard-docker _guard-venv
	$(PYTHON) backend/data/fetcher.py update

## fetch-debug: Same as fetch with DEBUG logging (shows every request)
fetch-debug: _guard-docker _guard-venv
	LOGURU_LEVEL=DEBUG $(PYTHON) backend/data/fetcher.py update

## retry-failed: Retry tickers that failed the last fetch (DATE=YYYY-MM-DD, default: today)
retry-failed: _guard-docker _guard-venv
	$(PYTHON) backend/data/fetcher.py retry-failed $(if $(DATE),--date $(DATE),)

## hydrate-indicators: Backfill full indicator history for all tickers (required before backtest)
hydrate-indicators: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m data.hydrate_indicators

## hydrate-fx: Backfill USDCAD FX rate history from 2017
hydrate-fx: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m data.hydrate_fx_rates

## fetch-vix: Backfill VIX history from 2017-01-02 (required before backtest)
fetch-vix: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m data.fetch_vix


# -----------------------------------------------------------------------------
# Backtesting   (caffeinate keeps the Mac awake for the full run)
#
#   WORKERS=N         — parallel workers (default: 1 = sequential)
#   CAPITAL_MODE=…    — capital_refresh | capital_compounded | all (default: all)
#   CONFIG=name       — config name for backtest-config / backtest-config-all-modes
# -----------------------------------------------------------------------------

## backtest: Run all configs × both capital modes
backtest: _guard-docker _guard-venv
	$(BACKTEST) $(if $(CAPITAL_MODE),--capital-mode $(CAPITAL_MODE),) $(if $(WORKERS),--workers $(WORKERS),)'

## backtest-config: Run one config × both capital modes  (CONFIG=name)
backtest-config: _guard-docker _guard-venv
	$(BACKTEST) --config $(CONFIG) $(if $(CAPITAL_MODE),--capital-mode $(CAPITAL_MODE),) $(if $(WORKERS),--workers $(WORKERS),)'

## backtest-config-all-modes: Run one config × both capital modes  (CONFIG=name)
backtest-config-all-modes: _guard-docker _guard-venv
	$(BACKTEST) --config $(CONFIG) --capital-mode all $(if $(WORKERS),--workers $(WORKERS),)'

## backtest-mode: Run all configs for one capital mode  (CAPITAL_MODE=…)
backtest-mode: _guard-docker _guard-venv
	$(BACKTEST) --capital-mode $(CAPITAL_MODE) $(if $(WORKERS),--workers $(WORKERS),)'


# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------

## help: Print all available targets
help:
	@echo ""
	@echo "Kairos — Algorithmic Trading System"
	@echo "====================================="
	@echo "First-time setup:  make install → make up → make setup → make run"
	@echo ""
	@echo "Targets:"
	@awk '/^## /{sub(/^## /,""); split($$0, a, ": "); printf "  %-30s %s\n", a[1], a[2]}' $(MAKEFILE_LIST)
	@echo ""


# -----------------------------------------------------------------------------
# Internal guards
# -----------------------------------------------------------------------------

_guard-docker:
	@if ! docker info > /dev/null 2>&1; then \
		echo "ERROR: Docker is not running. Start Docker Desktop and try again."; \
		exit 1; \
	fi
	@if ! docker ps --format '{{.Names}}' | grep -q '^kairos_db$$'; then \
		echo "ERROR: kairos_db container is not running. Run 'make up' first."; \
		exit 1; \
	fi

_guard-venv:
	@if [ ! -d "$(VENV)" ]; then \
		echo "ERROR: Virtual environment not found at $(VENV). Run 'make install' first."; \
		exit 1; \
	fi
