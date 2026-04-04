"""
data/hydrate_fx_rates.py — Backfill the fx_rates table with historical FX data.

Downloads daily OHLCV bars for each configured FX pair from 2017-01-02 to today,
timestamps each bar at 9:31 AM ET (the market-open anchor used throughout Kairos),
and upserts them into the fx_rates table via db.connection.insert_fx_rate().

Usage
-----
    # From the project root (venv must be active):
    cd backend && python -m data.hydrate_fx_rates

    # Or via the project venv directly:
    backend/.venv/bin/python backend/data/hydrate_fx_rates.py

Pairs hydrated by default
--------------------------
    USDCAD — 1 USD = X CAD  (needed when PORTFOLIO_CURRENCY=CAD and holding USD assets)
    CADUSD — 1 CAD = X USD  (needed when PORTFOLIO_CURRENCY=USD and holding CAD assets)

Both pairs are derived from the same yfinance ticker (USDCAD=X); the CADUSD
rate is simply 1/USDCAD so only one network call is made.

Constraints honoured
---------------------
- backend/.venv only — never system Python
- All timestamps UTC
- db/connection.py is the only file that writes to DB
- yfinance==1.2.0 pinned, do NOT upgrade
"""

import os
import sys
import time as _time_module

# ---------------------------------------------------------------------------
# Must be running inside the project venv
# ---------------------------------------------------------------------------
if sys.prefix == sys.base_prefix:
    print(
        "ERROR: Not running inside the project virtual environment.\n"
        "Activate it with:  source backend/.venv/bin/activate\n"
        "Or run via:        backend/.venv/bin/python backend/data/hydrate_fx_rates.py"
    )
    sys.exit(1)

# Insert backend/ onto sys.path so package imports work
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf
from loguru import logger

from db.connection import insert_fx_rate, ping

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")

_START_DATE = date(2017, 1, 2)

# yfinance ticker → (pair name stored in DB)
# PORTFOLIO_CURRENCY=CAD → only USDCAD is needed (1 USD = X CAD).
# Change this list if PORTFOLIO_CURRENCY ever changes to USD.
_PAIRS: list[tuple[str, str]] = [
    # (yfinance_ticker, pair_name)
    ("USDCAD=X", "USDCAD"),
]

_OPEN_HOUR   = 9
_OPEN_MINUTE = 31


def _to_open_utc(day: date) -> datetime:
    """Return 9:31 AM ET on *day* as a UTC datetime."""
    naive_et = datetime(day.year, day.month, day.day, _OPEN_HOUR, _OPEN_MINUTE, 0)
    aware_et = naive_et.replace(tzinfo=_ET)
    return aware_et.astimezone(timezone.utc)


def _fetch_pair(yf_ticker: str, start: date) -> "pd.DataFrame":
    """Download daily OHLCV from yfinance for *yf_ticker* back to *start*."""
    import pandas as pd

    end_str   = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")  # exclusive upper bound → includes today
    start_str = start.strftime("%Y-%m-%d")
    logger.info(f"Downloading {yf_ticker} from {start_str} to {end_str} ...")

    df = yf.download(
        yf_ticker,
        start=start_str,
        end=end_str,
        interval="1d",
        progress=False,
        auto_adjust=True,
    )

    if df is None or df.empty:
        logger.warning(f"No data returned for {yf_ticker}")
        return pd.DataFrame()

    # Normalise index to UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    return df


def hydrate(dry_run: bool = False) -> int:
    """Fetch all configured pairs and upsert into fx_rates.

    Parameters
    ----------
    dry_run:
        If True, print what would be inserted without touching the DB.

    Returns
    -------
    int
        Total number of rows upserted (or would-be upserted on dry_run).
    """
    if not dry_run and not ping():
        logger.error("Database is not reachable. Start TimescaleDB first: make up")
        sys.exit(1)

    total_rows = 0

    for yf_ticker, pair in _PAIRS:
        df = _fetch_pair(yf_ticker, _START_DATE)
        if df.empty:
            continue

        # yfinance single-ticker download returns flat columns (Open/Close/…).
        # MultiIndex can appear if yfinance wraps columns — unwrap to a Series.
        if hasattr(df.columns, "levels"):
            try:
                df = df["Close"].iloc[:, 0]
            except (KeyError, IndexError):
                df = df.iloc[:, 0]
        else:
            df = df["Close"]

        df = df.dropna()
        if df.empty:
            logger.warning(f"No close prices available for {yf_ticker}")
            continue

        rows_this_pair = 0
        for bar_ts, close_price in df.items():
            bar_date = bar_ts.date()
            if bar_date < _START_DATE:
                continue

            open_utc = _to_open_utc(bar_date)
            rate = float(close_price)

            if dry_run:
                logger.info(
                    f"[dry-run] {pair} {bar_date} @9:31ET → "
                    f"{open_utc.strftime('%Y-%m-%d %H:%M UTC')} rate={rate:.6f}"
                )
            else:
                insert_fx_rate(pair, rate, at_time=open_utc)

            rows_this_pair += 1

        action = "Would insert" if dry_run else "Upserted"
        logger.info(f"{action} {rows_this_pair} rows for {pair}")
        total_rows += rows_this_pair

        # Be polite to yfinance rate limits when multiple pairs are configured
        _time_module.sleep(0.5)

    return total_rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Backfill fx_rates table from 2017-01-02 to today."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be inserted without writing to the database.",
    )
    args = parser.parse_args()

    t0 = _time_module.time()
    n  = hydrate(dry_run=args.dry_run)
    elapsed = _time_module.time() - t0

    action = "Would upsert" if args.dry_run else "Upserted"
    print(f"\n{action} {n} total rows in {elapsed:.1f}s")
    if not args.dry_run:
        print("fx_rates table hydrated successfully.")
