"""
data/hydrate_fx_rates.py — Backfill the fx_rates table with historical FX data.

Strategy
--------
The live system fetches the FX bar at 9:31 AM ET (the open-cutoff used by
executor.fetch_fx_rate). For backtest consistency, historical FX rates should
ideally be anchored at the same 9:31 ET point. However, yfinance only serves
1-minute bars for the last ~60 days. For older history we fall back to the
daily close (~16:00 ET → ~21:00 UTC depending on DST) and tag the row with
``source='yfinance_daily_close'`` so analytics can distinguish the two
anchors.

Algorithm
---------
1. Walk the date range in 30-day chunks from ``_START_DATE`` to today.
2. For chunks ending within ``_INTRADAY_WINDOW_DAYS`` of today, try
   ``interval='1m'`` first. For each trading day in the chunk, pick the bar
   at or before 9:31 ET and stamp it at exactly that UTC moment.
3. For older chunks (or chunks where 1m returned empty), fetch
   ``interval='1d'`` and stamp at 16:00 ET (the daily close) converted to UTC.
4. Idempotent: existing ``ON CONFLICT (time, pair) DO UPDATE SET rate``
   means re-running is safe and updates rates to the most recent fetch.

Usage
-----
    cd backend && python -m data.hydrate_fx_rates             # full backfill
    cd backend && python -m data.hydrate_fx_rates --dry-run   # no writes
    cd backend && python -m data.hydrate_fx_rates --since 2024-01-01

Pairs hydrated
--------------
    USDCAD — 1 USD = X CAD  (used when PORTFOLIO_CURRENCY=CAD with USD assets)

Constraints
-----------
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

import pandas as pd
import yfinance as yf
from loguru import logger

from db.connection import insert_fx_rate, ping
from utils.trading_calendar import is_nyse_trading_day

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")

_START_DATE = date(2017, 1, 2)

# yfinance ticker → (pair name stored in DB)
# PORTFOLIO_CURRENCY=CAD → only USDCAD is needed (1 USD = X CAD).
_PAIRS: list[tuple[str, str]] = [
    ("USDCAD=X", "USDCAD"),
]

_OPEN_HOUR    = 9
_OPEN_MINUTE  = 31
_CLOSE_HOUR   = 16
_CLOSE_MINUTE = 0

# yfinance allows 1-min bars only for roughly the last 60 days. Be
# conservative (55) so we don't waste API calls on chunks we know will return
# empty, while still preferring intraday data when it's available.
_INTRADAY_WINDOW_DAYS = 55

# How many calendar days to request at a time when fetching 1-min bars.
# yfinance refuses spans longer than 30 days for ``interval='1m'``.
_CHUNK_DAYS = 30

# Retry backoff for transient yfinance errors (seconds).
_BACKOFF = [2, 4, 8]

# Source tags
_SOURCE_INTRADAY     = "yfinance_1m"
_SOURCE_DAILY_CLOSE  = "yfinance_daily_close"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _to_open_utc(day: date) -> datetime:
    """Return 9:31 AM ET on *day* as a UTC datetime (mirrors executor._open_cutoff_utc)."""
    return datetime(
        day.year, day.month, day.day,
        _OPEN_HOUR, _OPEN_MINUTE, tzinfo=_ET,
    ).astimezone(timezone.utc)


def _to_close_utc(day: date) -> datetime:
    """Return 16:00 ET on *day* (NYSE close) as a UTC datetime.

    Used to stamp daily-close fallback rates so the timestamp matches the
    actual moment the rate was observed.
    """
    return datetime(
        day.year, day.month, day.day,
        _CLOSE_HOUR, _CLOSE_MINUTE, tzinfo=_ET,
    ).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# yfinance wrappers
# ---------------------------------------------------------------------------

def _yf_download(yf_ticker: str, **kwargs) -> pd.DataFrame:
    """Run yf.download with exponential backoff; return empty DataFrame on final failure."""
    attempts = [None] + _BACKOFF
    for attempt_idx, wait in enumerate(attempts):
        if wait is not None:
            logger.warning(
                f"{yf_ticker}: retrying in {wait}s "
                f"(attempt {attempt_idx + 1}/{len(attempts)})..."
            )
            _time_module.sleep(wait)
        try:
            df = yf.download(yf_ticker, progress=False, auto_adjust=True, **kwargs)
            return df if df is not None else pd.DataFrame()
        except Exception as exc:
            if attempt_idx >= len(attempts) - 1:
                logger.error(f"{yf_ticker}: all retries exhausted: {exc}")
                return pd.DataFrame()
            logger.warning(f"{yf_ticker}: fetch error: {exc}")
    return pd.DataFrame()


def _flatten_close(df: pd.DataFrame) -> "pd.Series | None":
    """Reduce a yfinance DataFrame (with potentially multi-level columns) to a Close Series."""
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        try:
            s = df["Close"]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
        except (KeyError, IndexError):
            s = df.iloc[:, 0]
    else:
        s = df["Close"]
    return s.dropna()


def _normalise_index_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Treat naive vendor timestamps as America/New_York; convert to UTC."""
    if df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


# ---------------------------------------------------------------------------
# Per-chunk fetch
# ---------------------------------------------------------------------------

def _fetch_intraday_chunk(yf_ticker: str, chunk_start: date, chunk_end: date) -> "pd.Series | None":
    """Fetch 1-min Close bars for a chunk. Returns a UTC-indexed Series or None."""
    end_exclusive = chunk_end + timedelta(days=1)
    df = _yf_download(
        yf_ticker,
        start=chunk_start.isoformat(),
        end=end_exclusive.isoformat(),
        interval="1m",
    )
    df = _normalise_index_utc(df)
    return _flatten_close(df)


def _fetch_daily_chunk(yf_ticker: str, chunk_start: date, chunk_end: date) -> "pd.Series | None":
    """Fetch 1-day Close bars for a chunk. Returns a UTC-indexed Series or None."""
    end_exclusive = chunk_end + timedelta(days=1)
    df = _yf_download(
        yf_ticker,
        start=chunk_start.isoformat(),
        end=end_exclusive.isoformat(),
        interval="1d",
    )
    df = _normalise_index_utc(df)
    return _flatten_close(df)


# ---------------------------------------------------------------------------
# Chunked backfill
# ---------------------------------------------------------------------------

def _iter_chunks(start: date, end: date, chunk_days: int):
    """Yield (chunk_start, chunk_end) tuples covering [start, end] inclusive."""
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _pick_intraday_value(series: "pd.Series", trade_day: date) -> "float | None":
    """Return the last 1-min bar at or before 9:31 ET on *trade_day*."""
    if series is None or series.empty:
        return None
    cutoff = _to_open_utc(trade_day)
    day_start_utc = _to_open_utc(trade_day).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )  # exclusive lower bound to limit each day to its own bars
    mask = (series.index >= day_start_utc) & (series.index <= cutoff)
    day_bars = series[mask]
    if day_bars.empty:
        return None
    return float(day_bars.iloc[-1])


def _pick_daily_value(series: "pd.Series", trade_day: date) -> "float | None":
    """Return the daily-close value for *trade_day* (matched by date)."""
    if series is None or series.empty:
        return None
    day_bars = series[series.index.date == trade_day]
    if day_bars.empty:
        return None
    # Most recent bar for that calendar day (yfinance 1d gives one bar per day)
    return float(day_bars.iloc[-1])


def hydrate(
    *,
    dry_run: bool = False,
    since: "date | None" = None,
) -> dict:
    """Fetch all configured pairs and upsert into fx_rates.

    Parameters
    ----------
    dry_run:
        Print what would be inserted; don't touch the DB.
    since:
        Optional override of ``_START_DATE``. Useful for incremental top-ups.

    Returns
    -------
    dict with per-pair counts: ``{pair: {'intraday': int, 'daily': int, 'skipped': int}}``.
    """
    if not dry_run and not ping():
        logger.error("Database is not reachable. Start TimescaleDB first: make up")
        sys.exit(1)

    start_date  = since or _START_DATE
    today       = date.today()
    intraday_threshold = today - timedelta(days=_INTRADAY_WINDOW_DAYS)

    stats: dict[str, dict[str, int]] = {}

    for yf_ticker, pair in _PAIRS:
        logger.info(f"=== {pair} ({yf_ticker}) : {start_date} → {today} ===")
        per_pair = {"intraday": 0, "daily": 0, "skipped": 0}

        for chunk_start, chunk_end in _iter_chunks(start_date, today, _CHUNK_DAYS):
            # Decide whether to attempt intraday for this chunk
            try_intraday = chunk_end >= intraday_threshold

            intraday_series = None
            if try_intraday:
                intraday_series = _fetch_intraday_chunk(yf_ticker, chunk_start, chunk_end)
                if intraday_series is None or intraday_series.empty:
                    intraday_series = None

            # Always fetch the daily series as a fallback (covers gaps within the
            # intraday window too — e.g. weekends or thin-data days).
            daily_series = _fetch_daily_chunk(yf_ticker, chunk_start, chunk_end)

            # Walk every calendar day in the chunk; only act on trading days.
            current = chunk_start
            while current <= chunk_end:
                if not is_nyse_trading_day(current):
                    current += timedelta(days=1)
                    continue

                rate = None
                source = None
                stamp_utc = None

                if intraday_series is not None:
                    rate = _pick_intraday_value(intraday_series, current)
                    if rate is not None:
                        source = _SOURCE_INTRADAY
                        stamp_utc = _to_open_utc(current)

                if rate is None and daily_series is not None:
                    rate = _pick_daily_value(daily_series, current)
                    if rate is not None:
                        source = _SOURCE_DAILY_CLOSE
                        stamp_utc = _to_close_utc(current)

                if rate is None:
                    per_pair["skipped"] += 1
                    logger.debug(f"{pair} {current}: no data — skipped")
                    current += timedelta(days=1)
                    continue

                if dry_run:
                    logger.info(
                        f"[dry-run] {pair} {current} {source} → "
                        f"{stamp_utc.strftime('%Y-%m-%d %H:%M UTC')} rate={rate:.6f}"
                    )
                else:
                    insert_fx_rate(pair, rate, at_time=stamp_utc, source=source)

                if source == _SOURCE_INTRADAY:
                    per_pair["intraday"] += 1
                else:
                    per_pair["daily"] += 1

                current += timedelta(days=1)

            _time_module.sleep(0.5)  # gentle throttle between chunks

        stats[pair] = per_pair
        logger.info(
            f"{pair}: {per_pair['intraday']} intraday + {per_pair['daily']} daily + "
            f"{per_pair['skipped']} skipped"
        )

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Backfill fx_rates table. Anchors at 9:31 ET (intraday) "
                    "or 16:00 ET (daily-close fallback) depending on data availability.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be inserted without writing to the database.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Start date for backfill (default: 2017-01-02).",
    )
    args = parser.parse_args()

    since_arg = None
    if args.since:
        try:
            since_arg = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}")
            sys.exit(1)

    t0 = _time_module.time()
    stats = hydrate(dry_run=args.dry_run, since=since_arg)
    elapsed = _time_module.time() - t0

    print()
    print("=" * 56)
    total_intraday = sum(s["intraday"] for s in stats.values())
    total_daily    = sum(s["daily"]    for s in stats.values())
    total_skipped  = sum(s["skipped"]  for s in stats.values())
    action = "Would upsert" if args.dry_run else "Upserted"
    print(f"  {action} {total_intraday} intraday + {total_daily} daily "
          f"= {total_intraday + total_daily} total rows")
    print(f"  Skipped {total_skipped} trading days (no data available)")
    print(f"  Wall time: {elapsed:.1f}s")
    print("=" * 56)
