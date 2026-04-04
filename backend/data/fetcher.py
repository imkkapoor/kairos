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
    log_daily_fetch,
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

_RATE_DELAY = 0         # seconds between successive ticker downloads
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

def backfill(ticker: str, interval: str = "1d", years: int = 10) -> dict:
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
    dict
        {status: 'success'|'error', rows: int, ticker: str}
    """
    today      = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=years * 365)
    start      = start_date.isoformat()
    end        = (today + timedelta(days=1)).isoformat()  # yfinance end is exclusive

    logger.debug(f"[{ticker}] backfill {start} → {today} ({interval})")

    df = _fetch_with_retry(ticker, start=start, end=end, interval=interval)
    df = _normalise(df, ticker)

    if df.empty:
        logger.warning(f"[{ticker}] empty DataFrame after fetch/normalise")
        return {"status": "error", "rows": 0, "ticker": ticker}

    try:
        rows = insert_price_data(df, ticker, interval)
        logger.debug(f"[{ticker}] backfill inserted {rows} rows")
        return {"status": "success", "rows": rows, "ticker": ticker}
    except Exception as exc:
        logger.error(f"[{ticker}] DB insert error during backfill: {exc}")
        return {"status": "error", "rows": 0, "ticker": ticker}


def update(ticker: str, interval: str = "1d") -> dict:
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
    dict
        {status: 'success'|'skipped'|'error', rows: int, ticker: str}
    """
    latest = get_latest_timestamp(ticker, interval)
    if latest is None:
        logger.trace(f"[{ticker}] no existing data — running backfill")
        return backfill(ticker, interval)

    today      = datetime.now(timezone.utc).date()
    start_date = (latest + timedelta(days=1)).date()

    if start_date > today:
        logger.trace(f"[{ticker}] already up-to-date (latest: {latest.date()})")
        return {"status": "skipped", "rows": 0, "ticker": ticker}

    start = start_date.isoformat()
    end   = (today + timedelta(days=1)).isoformat()  # yfinance end is exclusive

    logger.trace(f"[{ticker}] update {start} → {today} ({interval})")

    df = _fetch_with_retry(ticker, start=start, end=end, interval=interval)
    df = _normalise(df, ticker)

    if df.empty:
        if start_date >= today:
            logger.trace(f"[{ticker}] no new daily bar yet (market may still be open)")
            return {"status": "skipped", "rows": 0, "ticker": ticker}
        logger.warning(f"[{ticker}] empty DataFrame after fetch/normalise")
        return {"status": "error", "rows": 0, "ticker": ticker}

    try:
        rows = insert_price_data(df, ticker, interval)
        logger.trace(f"[{ticker}] update inserted {rows} rows")
        return {"status": "success", "rows": rows, "ticker": ticker}
    except Exception as exc:
        logger.error(f"[{ticker}] DB insert error during update: {exc}")
        return {"status": "error", "rows": 0, "ticker": ticker}


def backfill_all(interval: str = "1d", years: int = 10) -> None:
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

    t_start   = _time.monotonic()
    n_success = n_skipped = n_failed = n_rows = 0
    failed_list: list[str] = []

    def _run(ticker: str) -> None:
        nonlocal n_success, n_skipped, n_failed, n_rows
        try:
            result = backfill(ticker, interval, years)
        except Exception as exc:
            logger.error(f"[{ticker}] unexpected error: {exc}")
            result = {"status": "error", "rows": 0, "ticker": ticker}
        if result["status"] == "success":
            n_success += 1
            n_rows    += result["rows"]
        elif result["status"] == "skipped":
            n_skipped += 1
        else:
            n_failed += 1
            failed_list.append(ticker)

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
                _run(ticker)
                _time.sleep(_RATE_DELAY)
                progress.advance(task)
    else:
        for i, ticker in enumerate(tickers, 1):
            logger.info(f"[{i}/{len(tickers)}] {ticker}")
            _run(ticker)
            _time.sleep(_RATE_DELAY)

    duration = _time.monotonic() - t_start
    log_daily_fetch(
        tickers_total   = len(tickers),
        tickers_success = n_success,
        tickers_skipped = n_skipped,
        tickers_failed  = n_failed,
        failed_tickers  = failed_list,
        rows_inserted   = n_rows,
        duration_secs   = round(duration, 2),
    )
    logger.info(
        f"backfill_all complete — {n_success} ok, {n_skipped} skipped, "
        f"{n_failed} failed, {n_rows} rows inserted in {duration:.1f}s"
    )


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

    t_start   = _time.monotonic()
    n_success = n_skipped = n_failed = n_rows = 0
    failed_list: list[str] = []

    def _run_update(ticker: str) -> None:
        nonlocal n_success, n_skipped, n_failed, n_rows
        try:
            result = update(ticker, interval)
        except Exception as exc:
            logger.error(f"[{ticker}] unexpected error: {exc}")
            result = {"status": "error", "rows": 0, "ticker": ticker}
        if result["status"] == "success":
            n_success += 1
            n_rows    += result["rows"]
        elif result["status"] == "skipped":
            n_skipped += 1
        else:
            n_failed += 1
            failed_list.append(ticker)

    if _RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Updating...", total=len(tickers))
            for ticker in tickers:
                progress.update(task, description=f"[cyan]{ticker}[/cyan]")
                _run_update(ticker)
                _time.sleep(_RATE_DELAY)
                progress.advance(task)
    else:
        for i, ticker in enumerate(tickers, 1):
            logger.info(f"[{i}/{len(tickers)}] {ticker}")
            _run_update(ticker)
            _time.sleep(_RATE_DELAY)

    duration = _time.monotonic() - t_start
    log_daily_fetch(
        tickers_total   = len(tickers),
        tickers_success = n_success,
        tickers_skipped = n_skipped,
        tickers_failed  = n_failed,
        failed_tickers  = failed_list,
        rows_inserted   = n_rows,
        duration_secs   = round(duration, 2),
    )
    logger.info(
        f"update_all complete — {n_success} ok, {n_skipped} skipped, "
        f"{n_failed} failed, {n_rows} rows inserted in {duration:.1f}s"
    )
    return n_rows


# ---------------------------------------------------------------------------
# Retry failed tickers from fetch_log
# ---------------------------------------------------------------------------

def retry_failed(date_str: str | None = None, interval: str = "1d") -> None:
    """Backfill any tickers that failed on a given calendar date.

    Reads ``failed_tickers`` from the fetch_log row for *date_str*
    (YYYY-MM-DD, defaults to today UTC) and runs a full backfill() for each.
    Updates the fetch_log row with the new counts when done.

    Parameters
    ----------
    date_str:
        Calendar date to look up (e.g. "2026-03-24").  Defaults to today UTC.
    interval:
        Bar interval (default "1d").
    """
    from db.connection import get_fetch_history  # local import avoids circular at module level

    if date_str is None:
        date_str = _time.strftime("%Y-%m-%d", _time.gmtime())

    logger.info(f"retry_failed: looking up fetch_log for {date_str}")

    df = get_fetch_history(days=90)
    if df.empty:
        logger.warning("fetch_log is empty — nothing to retry")
        return

    # Find rows whose fetch_time falls on the requested calendar date (UTC)
    row = df[df["fetch_time"].dt.date.astype(str) == date_str]
    if row.empty:
        logger.warning(f"No fetch_log entry found for {date_str}")
        return

    failed_str = row.iloc[0]["failed_tickers"]
    if not failed_str or (isinstance(failed_str, float)):  # NULL comes back as float NaN
        logger.info(f"No failed tickers recorded for {date_str} — nothing to do")
        return

    tickers = [t.strip() for t in str(failed_str).split(",") if t.strip()]
    if not tickers:
        logger.info(f"No failed tickers for {date_str}")
        return

    logger.info(f"retry_failed: {len(tickers)} tickers to retry: {tickers}")

    t_start   = _time.monotonic()
    n_success = n_failed = n_rows = 0
    still_failed: list[str] = []

    for ticker in tickers:
        try:
            result = backfill(ticker, interval)
        except Exception as exc:
            logger.error(f"[{ticker}] unexpected error: {exc}")
            result = {"status": "error", "rows": 0, "ticker": ticker}
        if result["status"] == "success":
            n_success += 1
            n_rows    += result["rows"]
        else:
            n_failed += 1
            still_failed.append(ticker)
        _time.sleep(_RATE_DELAY)

    duration = _time.monotonic() - t_start

    # Append a new log row summarising the retry run
    original = row.iloc[0]
    log_daily_fetch(
        tickers_total   = int(original["tickers_total"]),
        tickers_success = int(original["tickers_success"]) + n_success,
        tickers_skipped = int(original["tickers_skipped"]),
        tickers_failed  = n_failed,
        failed_tickers  = still_failed,
        rows_inserted   = int(original["rows_inserted"]) + n_rows,
        duration_secs   = round(float(original["duration_secs"] or 0) + duration, 2),
        notes           = f"retried {len(tickers)} failed; {n_success} recovered",
    )
    logger.info(
        f"retry_failed complete — {n_success} recovered, {n_failed} still failing, "
        f"{n_rows} rows inserted in {duration:.1f}s"
    )


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
    bp.add_argument("--years",    type=int, default=10,    help="Years of history (default: 10)")

    up = subparsers.add_parser("update", help="Incremental update since last stored bar")
    up.add_argument("--ticker",   type=str, help="Single ticker (omit for all watchlist tickers)")
    up.add_argument("--interval", type=str, default="1d",  help="Bar interval (default: 1d)")

    rp = subparsers.add_parser("retry-failed", help="Retry tickers that failed on a given date")
    rp.add_argument("--date",     type=str, default=None,  help="Date to retry (YYYY-MM-DD, default: today)")
    rp.add_argument("--interval", type=str, default="1d",  help="Bar interval (default: 1d)")

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

    elif args.command == "retry-failed":
        retry_failed(args.date, args.interval)
