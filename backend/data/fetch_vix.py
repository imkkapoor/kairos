"""
data/fetch_vix.py — One-time VIX history backfill.

Downloads ^VIX daily close from yfinance and upserts into vix_data via
db/connection.py. Run once manually or via:

    make fetch-vix

This module is NEVER called from the backtester — the backtester reads
from vix_data through db/connection.get_vix_range().
"""

import sys
from datetime import timezone
from pathlib import Path

import yfinance as yf
import pandas as pd
from loguru import logger
from dotenv import load_dotenv

# Ensure backend/ is on sys.path when invoked as -m data.fetch_vix
_BACKEND_DIR = Path(__file__).parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

load_dotenv(dotenv_path=_BACKEND_DIR / ".env")

from db.connection import insert_vix_data


def fetch_and_store_vix(start_date: str = "2017-01-02") -> None:
    """Download ^VIX daily close from yfinance and upsert into vix_data.

    Parameters
    ----------
    start_date:
        ISO date string for the earliest bar to download. Defaults to
        '2017-01-02' (the first date with indicator data in the DB).

    Returns
    -------
    None — logs rows inserted vs skipped to stdout.
    """
    logger.info(f"Downloading ^VIX from {start_date}…")

    df = yf.download("^VIX", start=start_date, auto_adjust=True, progress=False)
    if df.empty:
        logger.error("yfinance returned empty DataFrame for ^VIX. Aborting.")
        return

    # Handle multi-level columns (yfinance 1.x may return ticker-level labels)
    if isinstance(df.columns, pd.MultiIndex):
        df = df["Close"]
        if isinstance(df, pd.DataFrame):
            df = df.iloc[:, 0]
    else:
        df = df["Close"]

    df = df.dropna()
    df.index.name = "time"
    df.name = "close"

    # Ensure UTC-aware DatetimeIndex
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    logger.info(f"Downloaded {len(df):,} rows for ^VIX ({start_date} → {df.index[-1].date()})")

    rows = [
        {"time": ts.to_pydatetime(), "close": float(close)}
        for ts, close in df.items()
    ]

    inserted = insert_vix_data(rows)
    skipped  = len(rows) - inserted
    logger.info(f"VIX backfill complete: {inserted:,} inserted, {skipped:,} skipped (already exist)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backfill VIX history from yfinance")
    parser.add_argument(
        "--start", default="2017-01-02", metavar="YYYY-MM-DD",
        help="Start date for VIX download (default: 2017-01-02)"
    )
    args = parser.parse_args()
    fetch_and_store_vix(start_date=args.start)
