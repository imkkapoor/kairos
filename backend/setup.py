"""
setup.py — One-time initialisation script for Kairos.

Run via:  make setup

Steps
-----
1. Assert running inside the project venv (not system Python)
2. Verify Docker daemon is responsive
3. Start TimescaleDB container and wait for it to accept connections
4. Copy backend/.env.example → backend/.env (if not already present)
5. Fetch S&P 500 constituents from Wikipedia (~503 tickers)
6. Fetch TSX 60 constituents from Wikipedia (~60 tickers)
7. seed_watchlist() with the combined ~563 tickers
8. backfill_all() — 5 years of daily OHLCV for every ticker
9. Print completion summary
"""

import os
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Step 1: must be running inside the project venv, not system Python
# ---------------------------------------------------------------------------
if sys.prefix == sys.base_prefix:
    print(
        "ERROR: Not running inside a virtual environment.\n"
        "Run 'make install' first, then 'make setup'."
    )
    sys.exit(1)

# Now safe to import third-party packages
import io

import pandas as pd
import requests
from loguru import logger

try:
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

# Insert backend/ onto sys.path so relative package imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.connection import ping, seed_watchlist  # noqa: E402
from data.fetcher import update_all             # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_docker() -> bool:
    """Return True if the Docker daemon is running and responsive."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _start_db() -> None:
    """Bring up the TimescaleDB container in detached mode."""
    logger.info("Starting TimescaleDB container...")
    subprocess.run(["docker", "compose", "up", "-d"], check=True)


def _wait_for_db(retries: int = 30, delay: float = 2.0) -> None:
    """Block until the database accepts connections or retries are exhausted."""
    logger.info(f"Waiting for database to become ready (up to {int(retries * delay)}s)...")
    for attempt in range(1, retries + 1):
        if ping():
            logger.info("Database is ready.")
            return
        logger.info(f"  [{attempt}/{retries}] not ready yet — retrying in {delay:.0f}s")
        time.sleep(delay)

    logger.error("Database did not become ready. Run 'make logs' to inspect the container.")
    sys.exit(1)


def _fetch_sp500() -> list[dict]:
    """Fetch S&P 500 constituents from Wikipedia.

    Returns a list of dicts with keys: ticker, name, sector, market.
    Cleans BRK.B-style tickers to BRK-B (yfinance convention).
    """
    logger.info("Fetching S&P 500 from Wikipedia...")
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=_HEADERS, timeout=15).text
    tables = pd.read_html(io.StringIO(html))
    df = tables[0][["Symbol", "Security", "GICS Sector"]].copy()
    df.columns = ["ticker", "name", "sector"]
    df["ticker"] = df["ticker"].str.strip().str.replace(".", "-", regex=False)
    df["market"] = "US"
    return df.to_dict("records")


def _load_extra_tickers() -> list[dict]:
    """Load extra tickers from watchlist_extra.csv (ETFs, custom stocks, etc.).

    The CSV lives at backend/watchlist_extra.csv. Add any row you want tracked
    by Kairos — ETFs, individual stocks, whatever. Required column: ``ticker``.
    Optional: ``name``, ``sector``, ``market`` (default US), ``notes``.
    Blank lines and lines starting with # are ignored.
    """
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist_extra.csv")
    if not os.path.exists(csv_path):
        logger.info("watchlist_extra.csv not found — skipping extra tickers")
        return []

    df = pd.read_csv(csv_path, comment="#", skip_blank_lines=True, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    if "ticker" not in df.columns:
        logger.warning("watchlist_extra.csv has no 'ticker' column — skipping")
        return []

    df = df.where(pd.notna(df), None)  # convert NaN → None for clean DB inserts
    results = []
    for _, row in df.iterrows():
        ticker = str(row["ticker"]).strip()
        if not ticker or ticker.lower() == "nan":
            continue
        results.append({
            "ticker": ticker,
            "name":   row.get("name"),
            "sector": row.get("sector"),
            "market": row.get("market") or "US",
            "notes":  row.get("notes"),
        })
    return results


def _fetch_tsx60() -> list[dict]:
    """Fetch TSX 60 constituents from Wikipedia.

    Returns a list of dicts with keys: ticker, name, sector, market.
    Appends .TO suffix to all tickers.
    """
    logger.info("Fetching TSX 60 from Wikipedia...")
    url = "https://en.wikipedia.org/wiki/S%26P/TSX_60"
    html = requests.get(url, headers=_HEADERS, timeout=15).text
    tables = pd.read_html(io.StringIO(html))
    df = tables[1]

    # Identify columns by name — Wikipedia table structure can vary
    ticker_col = sector_col = name_col = None
    for col in df.columns:
        cl = str(col).lower()
        if "symbol" in cl or "ticker" in cl:
            ticker_col = col
        elif "company" in cl or "name" in cl or "constituent" in cl:
            name_col = col
        elif "sector" in cl or "gics" in cl or "industry" in cl:
            sector_col = col

    if ticker_col is None:
        ticker_col = df.columns[0]  # fallback: first column

    results = []
    for _, row in df.iterrows():
        raw_ticker = str(row[ticker_col]).strip()
        if not raw_ticker or raw_ticker.lower() in ("nan", "symbol", "ticker", "—"):
            continue
        # Wikipedia uses dots for share classes (BIP.UN, CCL.B, TECK.B, etc.)
        # yfinance requires dashes instead: BIP-UN.TO, CCL-B.TO, TECK-B.TO
        clean_ticker = raw_ticker.replace(".", "-") + ".TO"
        results.append(
            {
                "ticker": clean_ticker,
                "name":   str(row[name_col]).strip()   if name_col   else None,
                "sector": str(row[sector_col]).strip() if sector_col else None,
                "market": "CA",
            }
        )
    return results


# ---------------------------------------------------------------------------
# Blacklist — tickers to exclude even if found on Wikipedia or in the CSV
# ---------------------------------------------------------------------------
# Add any ticker that causes yfinance errors, returns garbage data, or is
# otherwise unwanted. Exact match, case-sensitive.
_TICKER_BLACKLIST: set[str] = {
    "Q",   # Quintiles/IQVIA old symbol — yfinance returns bad/empty data
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _setup_steps = [
        "Docker check",
        "Start DB + wait for ready",
        "Copy .env",
        "Fetch S&P 500",
        "Fetch TSX 60 + extras",
        "Seed watchlist",
        "Backfill OHLCV data",
    ]

    if _RICH_AVAILABLE:
        _progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        )
        _task = _progress.add_task("Setup...", total=len(_setup_steps))
        _progress.start()

        def _step(label: str) -> None:
            _progress.update(_task, description=f"[cyan]{label}[/cyan]")

        def _done() -> None:
            _progress.advance(_task)
    else:
        def _step(label: str) -> None:
            logger.info(label)

        def _done() -> None:
            pass

    # Step 2: Docker check
    _step("Checking Docker daemon")
    if not _check_docker():
        if _RICH_AVAILABLE:
            _progress.stop()
        logger.error("Docker is not running. Start Docker Desktop and try again.")
        sys.exit(1)
    logger.info("Docker daemon is running.")
    _done()

    # Step 3: Start DB + wait
    _step("Starting TimescaleDB + waiting for ready")
    _start_db()
    _wait_for_db()
    _done()

    # Step 4: Copy .env.example → .env
    _step("Copying .env")
    _dir        = os.path.dirname(os.path.abspath(__file__))
    env_path    = os.path.join(_dir, ".env")
    env_example = os.path.join(_dir, ".env.example")

    if not os.path.exists(env_path):
        shutil.copy(env_example, env_path)
        logger.info(f"Created backend/.env from .env.example")
    else:
        logger.info("backend/.env already exists — skipping copy")
    _done()

    # Steps 5 & 6: Fetch watchlist from Wikipedia
    tickers: list[dict] = []

    _step("Fetching S&P 500 from Wikipedia")
    try:
        sp500 = _fetch_sp500()
        logger.info(f"S&P 500: {len(sp500)} tickers fetched")
        tickers.extend(sp500)
    except Exception as exc:
        logger.error(f"Failed to fetch S&P 500 from Wikipedia: {exc}")
    _done()

    _step("Fetching TSX 60 + extra tickers")
    try:
        tsx60 = _fetch_tsx60()
        logger.info(f"TSX 60: {len(tsx60)} tickers fetched")
        tickers.extend(tsx60)
    except Exception as exc:
        logger.error(f"Failed to fetch TSX 60 from Wikipedia: {exc}")

    # Load extra tickers from watchlist_extra.csv (ETFs, custom stocks, etc.)
    extra = _load_extra_tickers()
    if extra:
        logger.info(f"watchlist_extra.csv: {len(extra)} tickers loaded")
        tickers.extend(extra)

    # Apply blacklist
    before = len(tickers)
    tickers = [t for t in tickers if t["ticker"] not in _TICKER_BLACKLIST]
    removed = before - len(tickers)
    if removed:
        logger.info(f"Blacklist removed {removed} ticker(s): {_TICKER_BLACKLIST & {t['ticker'] for t in tickers} ^ _TICKER_BLACKLIST}")

    if not tickers:
        if _RICH_AVAILABLE:
            _progress.stop()
        logger.error("No tickers were fetched — cannot seed watchlist. Aborting.")
        sys.exit(1)
    _done()

    # Step 7: Seed watchlist
    _step("Seeding watchlist")
    seed_watchlist(tickers)
    logger.info(f"Watchlist seeded with {len(tickers)} tickers")
    _done()

    # Step 8: Fetch only missing/new bars (skips tickers that are already up-to-date)
    _step("Backfilling OHLCV data (this will take a while…)")
    if _RICH_AVAILABLE:
        _progress.stop()  # stop the step bar so update_all's own bar renders cleanly
    logger.info("Fetching missing OHLCV data (~2015-present). Already-fetched tickers are skipped...")
    update_all(interval="1d")

    # Step 9: Summary
    print()
    print("=" * 56)
    print("  Kairos setup complete!")
    print(f"  Watchlist : {len(tickers)} tickers seeded (S&P 500 + TSX 60 + watchlist_extra.csv)")
    print("  Data      : ~11yr daily OHLCV backfilled (~2015–present)")
    print("  Next step : make run")
    print("=" * 56)
    print()


if __name__ == "__main__":
    main()
