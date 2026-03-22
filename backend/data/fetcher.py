"""
data/fetcher.py — yfinance OHLCV fetcher with rate limiting and error handling.

All timestamps are converted to UTC before any database writes.
Rate limit: 5 s between tickers.
Retry policy: exponential backoff at 2 s / 4 s / 8 s on transient errors.
"""

import argparse
import os
import sys
import time as _time
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from loguru import logger

# When run directly as `python backend/data/fetcher.py`, Python puts backend/data/ on
# sys.path instead of backend/.  Insert backend/ so that `db` and `data` are importable.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from db.connection import (  # noqa: E402
    get_latest_timestamp,
    get_watchlist,
    insert_price_data,
    log_fetch,
)

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

_RATE_DELAY = 1.0          # seconds between successive ticker downloads
_BACKOFF    = [2, 4, 8]    # retry wait times in seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_with_retry(
    ticker: str,
    start: str,
    end: str,
    interval: str,
) -> pd.DataFrame:
    """Download OHLCV data from yfinance with exponential backoff.

    Parameters
    ----------
    ticker:
        Ticker symbol (e.g. "AAPL").
    start:
        ISO date string for the start of the range, e.g. "2020-01-01".
    end:
        ISO date string for the end of the range (exclusive), e.g. "2025-01-01".
    interval:
        yfinance interval string, e.g. "1d".

    Returns
    -------
    pd.DataFrame
        Raw DataFrame from yfinance, or an empty DataFrame on final failure.
    """
    attempts = [None] + _BACKOFF  # first attempt + 3 retries
    for attempt_idx, wait in enumerate(attempts):
        if wait is not None:
            logger.warning(
                f"[{ticker}] retrying in {wait}s (attempt {attempt_idx + 1}/{len(attempts)})..."
            )
            _time.sleep(wait)
        try:
            df = yf.download(
                ticker,
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            return df
        except Exception as exc:
            if attempt_idx < len(attempts) - 1:
                logger.warning(f"[{ticker}] fetch error: {exc}")
            else:
                logger.error(f"[{ticker}] all retries exhausted: {exc}")
                return pd.DataFrame()

    return pd.DataFrame()  # unreachable, satisfies type checkers


def _normalise(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Flatten yfinance output into a clean OHLCV DataFrame with a UTC DatetimeIndex.

    Handles both flat and MultiIndex column layouts from yfinance >= 0.2.31.
    Returns an empty DataFrame if required columns are missing.
    """
    if df.empty:
        return df

    df = df.copy()

    # yfinance >= 0.2.31 returns MultiIndex columns even for single tickers:
    #   level 0 = price type (Open, High, …), level 1 = ticker symbol
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).lower().strip() for c in df.columns]

    # With auto_adjust=True the adjusted column is named 'close'; drop any leftover 'adj close'
    if "adj close" in df.columns:
        if "close" not in df.columns:
            df = df.rename(columns={"adj close": "close"})
        else:
            df = df.drop(columns=["adj close"])

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        logger.warning(f"[{ticker}] missing columns after normalise: {missing} — skipping")
        return pd.DataFrame()

    df = df[["open", "high", "low", "close", "volume"]].dropna()

    if df.empty:
        return df

    # Convert index to UTC.
    # yfinance returns ET-localised timestamps for US equities; naive timestamps
    # for daily bars should be treated as America/New_York per the timezone contract.
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df.index.name = "time"
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def backfill(ticker: str, interval: str = "1d", years: int = 11) -> int:
    """Fetch and store N years of historical OHLCV data for a single ticker.

    Parameters
    ----------
    ticker:
        Ticker symbol.
    interval:
        Bar interval (default "1d").
    years:
        Number of years to backfill (default 5).

    Returns
    -------
    int
        Number of rows inserted into price_data.
    """
    today      = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=years * 365)
    start      = start_date.isoformat()
    end        = today.isoformat()

    logger.info(f"[{ticker}] backfill {start} → {end} ({interval})")

    df = _fetch_with_retry(ticker, start=start, end=end, interval=interval)
    df = _normalise(df, ticker)

    if df.empty:
        log_fetch(ticker, interval, 0, "error", "empty DataFrame after fetch/normalise")
        return 0

    try:
        rows = insert_price_data(df, ticker, interval)
        log_fetch(ticker, interval, rows, "success")
        logger.info(f"[{ticker}] backfill inserted {rows} rows")
        return rows
    except Exception as exc:
        log_fetch(ticker, interval, 0, "error", str(exc))
        logger.error(f"[{ticker}] DB insert error during backfill: {exc}")
        return 0


def update(ticker: str, interval: str = "1d") -> int:
    """Incrementally update a ticker since its last stored timestamp.

    If no data exists in the DB, falls back to a full backfill().
    Uses datetime.now(timezone.utc).date() to avoid requesting future dates.

    Parameters
    ----------
    ticker:
        Ticker symbol.
    interval:
        Bar interval (default "1d").

    Returns
    -------
    int
        Number of rows inserted.
    """
    latest = get_latest_timestamp(ticker, interval)
    if latest is None:
        logger.info(f"[{ticker}] no existing data — running backfill")
        return backfill(ticker, interval)

    today      = datetime.now(timezone.utc).date()
    start_date = (latest + timedelta(days=1)).date()

    if start_date > today:
        logger.debug(f"[{ticker}] already up-to-date (latest: {latest.date()})")
        log_fetch(ticker, interval, 0, "success")
        return 0

    start = start_date.isoformat()
    end   = today.isoformat()

    logger.info(f"[{ticker}] update {start} → {end} ({interval})")

    df = _fetch_with_retry(ticker, start=start, end=end, interval=interval)
    df = _normalise(df, ticker)

    if df.empty:
        log_fetch(ticker, interval, 0, "error", "empty DataFrame after fetch/normalise")
        return 0

    try:
        rows = insert_price_data(df, ticker, interval)
        log_fetch(ticker, interval, rows, "success")
        logger.info(f"[{ticker}] update inserted {rows} rows")
        return rows
    except Exception as exc:
        log_fetch(ticker, interval, 0, "error", str(exc))
        logger.error(f"[{ticker}] DB insert error during update: {exc}")
        return 0


def backfill_all(interval: str = "1d", years: int = 11) -> None:
    """Backfill all active watchlist tickers, displaying a rich progress bar.

    Parameters
    ----------
    interval:
        Bar interval (default "1d").
    years:
        Years of history to fetch per ticker (default 5).
    """
    tickers = get_watchlist(active_only=True)
    logger.info(f"Starting backfill for {len(tickers)} tickers ({years}yr, {interval})")

    if _RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Backfilling...", total=len(tickers))
            for ticker in tickers:
                progress.update(task, description=f"[cyan]{ticker}[/cyan]")
                try:
                    backfill(ticker, interval, years)
                except Exception as exc:
                    logger.error(f"[{ticker}] unexpected error: {exc}")
                _time.sleep(_RATE_DELAY)
                progress.advance(task)
    else:
        for i, ticker in enumerate(tickers, 1):
            logger.info(f"[{i}/{len(tickers)}] {ticker}")
            try:
                backfill(ticker, interval, years)
            except Exception as exc:
                logger.error(f"[{ticker}] unexpected error: {exc}")
            _time.sleep(_RATE_DELAY)


def update_all(interval: str = "1d") -> int:
    """Incrementally update all active watchlist tickers.

    Parameters
    ----------
    interval:
        Bar interval (default "1d").

    Returns
    -------
    int
        Total number of rows inserted across all tickers.
    """
    tickers = get_watchlist(active_only=True)
    logger.info(f"Incremental update for {len(tickers)} tickers ({interval})")

    total = 0
    for ticker in tickers:
        try:
            rows = update(ticker, interval)
            total += rows
        except Exception as exc:
            logger.error(f"[{ticker}] unexpected error: {exc}")
        _time.sleep(_RATE_DELAY)

    logger.info(f"update_all complete — {total} rows inserted across all tickers")
    return total


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="fetcher",
        description="Kairos data fetcher — download OHLCV data via yfinance",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bp = subparsers.add_parser("backfill", help="Fetch full historical data")
    bp.add_argument("--ticker",   type=str, help="Single ticker (omit for all watchlist tickers)")
    bp.add_argument("--interval", type=str, default="1d",  help="Bar interval (default: 1d)")
    bp.add_argument("--years",    type=int, default=11,    help="Years of history (default: 11)")

    up = subparsers.add_parser("update", help="Incremental update since last stored bar")
    up.add_argument("--ticker",   type=str, help="Single ticker (omit for all watchlist tickers)")
    up.add_argument("--interval", type=str, default="1d",  help="Bar interval (default: 1d)")

    args = parser.parse_args()

    if args.command == "backfill":
        if args.ticker:
            backfill(args.ticker, args.interval, args.years)
        else:
            backfill_all(args.interval, args.years)

    elif args.command == "update":
        if args.ticker:
            update(args.ticker, args.interval)
        else:
            update_all(args.interval)
