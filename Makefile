VENV   := backend/.venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

.PHONY: install up down logs setup run scan fetch retry-failed simulate simulate-evening shell-db reset-db help \
        _guard-docker _guard-venv

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

## up: Start the TimescaleDB container in the background
up:
	docker compose up -d

## down: Stop the TimescaleDB container
down:
	docker compose down

## logs: Stream Docker container logs (Ctrl+C to stop)
logs:
	docker compose logs -f

## setup: Init schema, seed watchlist (S&P500 + TSX60 + watchlist_extra.csv), backfill OHLCV
setup: _guard-docker _guard-venv
	$(PYTHON) backend/setup.py

## run: Start the weekday scheduler (fetch + strategy stubs + health check)
run: _guard-docker _guard-venv
	$(PYTHON) backend/scheduler.py

## scan: Run the daily signal scan once right now (skips schedule)
scan: _guard-docker _guard-venv
	$(PYTHON) -c "import sys; sys.path.insert(0, 'backend'); from strategies.scanner import run_daily_scan; run_daily_scan()"

## simulate: Run morning execution now (uses last weekday's signals + live prices)
simulate: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m simulator.simulator morning

## simulate-evening: Run evening position management now (uses DB close prices)
simulate-evening: _guard-docker _guard-venv
	cd backend && $(abspath $(PYTHON)) -m simulator.simulator evening

## fetch: Incremental update for all active watchlist tickers
fetch:
	$(PYTHON) backend/data/fetcher.py update

## retry-failed: Retry tickers that failed in last fetch (DATE=YYYY-MM-DD, default: today)
retry-failed: _guard-docker _guard-venv
	$(PYTHON) backend/data/fetcher.py retry-failed $(if $(DATE),--date $(DATE),)

## shell-db: Open an interactive psql session inside kairos_db
shell-db:
	docker exec -it kairos_db psql -U kairos -d kairos

## reset-db: DESTRUCTIVE — wipe entire database and recreate from scratch
reset-db:
	@read -p "WARNING: This will DELETE all data and volumes. Type 'yes' to confirm: " _confirm; \
	if [ "$$_confirm" = "yes" ]; then \
		docker compose down -v; \
		docker compose up -d; \
		echo "Database reset complete."; \
	else \
		echo "Aborted — no changes made."; \
	fi

## help: Print this help message and first-time setup flow
help:
	@echo ""
	@echo "Kairos — Algorithmic Paper Trading System"
	@echo "========================================="
	@echo "First-time setup:  make install  →  make up  →  make setup  →  make run"
	@echo "Run scan now:      make scan"
	@echo ""
	@echo "Targets:"
	@awk '/^## /{sub(/^## /,""); split($$0, a, ": "); printf "  %-12s %s\n", a[1], a[2]}' $(MAKEFILE_LIST)
	@echo ""

# ---------------------------------------------------------------------------
# Internal guards — not meant to be called directly
# ---------------------------------------------------------------------------
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
