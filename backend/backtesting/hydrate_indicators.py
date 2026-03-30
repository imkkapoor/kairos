"""
backtesting/hydrate_indicators.py — Backfill the indicators table with 10 years of history.

Reads all OHLCV from price_data, computes 14 technical indicators per bar using
pandas_ta (same formulas as strategies/indicators.py), and bulk-upserts into
the indicators table.

Design:
  - Processes tickers in serial (pandas_ta is CPU-bound, parallelism gains are
    marginal and risk OOM). Each ticker takes ~0.5–2s for ~2500 bars.
  - DB writes are batched: one bulk INSERT per ticker using execute_values().
  - ON CONFLICT DO NOTHING makes it fully idempotent — safe to re-run.
  - Commits per ticker so a crash doesn't lose all progress.
  - Memory: only one ticker's DataFrame in memory at a time.

Usage:
    cd backend && .venv/bin/python -m backtesting.hydrate_indicators
    # or
    make hydrate-indicators
"""

import math
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta
from loguru import logger

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from db.connection import get_conn, get_engine, get_watchlist

try:
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    _RICH = True
except ImportError:
    _RICH = False

# Batch size for execute_values (rows per INSERT statement)
_BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Indicator computation (mirrors strategies/indicators.py exactly)
# ---------------------------------------------------------------------------

def _find_col(df: pd.DataFrame, prefix: str) -> str | None:
    for col in df.columns:
        if str(col).startswith(prefix):
            return col
    return None


def _compute_all_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all 14 indicators for every row in the OHLCV DataFrame.

    Returns the DataFrame with indicator columns appended.
    Rows where critical indicators are NaN (warmup period) are kept but will
    have NaN values — these are filtered out before DB insert.
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    df["rsi_14"] = ta.rsi(close, length=14)
    df["ma_50"] = ta.sma(close, length=50)
    df["ma_200"] = ta.sma(close, length=200)
    df["ema_20"] = ta.ema(close, length=20)

    # Bollinger Bands
    bb = ta.bbands(close, length=20, std=2.0)
    if bb is not None and not bb.empty:
        bbu = _find_col(bb, "BBU")
        bbm = _find_col(bb, "BBM")
        bbl = _find_col(bb, "BBL")
        df["bb_upper"] = bb[bbu] if bbu else float("nan")
        df["bb_mid"] = bb[bbm] if bbm else float("nan")
        df["bb_lower"] = bb[bbl] if bbl else float("nan")
    else:
        df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = float("nan")

    df["atr_14"] = ta.atr(high, low, close, length=14)

    adx_df = ta.adx(high, low, close, length=14)
    if adx_df is not None and not adx_df.empty:
        adx_col = _find_col(adx_df, "ADX_")
        df["adx_14"] = adx_df[adx_col] if adx_col else float("nan")
    else:
        df["adx_14"] = float("nan")

    df["volume_sma"] = ta.sma(volume, length=20)

    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is not None and not macd_df.empty:
        macd_col = _find_col(macd_df, "MACD_")
        macds_col = _find_col(macd_df, "MACDs_")
        macdh_col = _find_col(macd_df, "MACDh_")
        df["macd_line"] = macd_df[macd_col] if macd_col else float("nan")
        df["macd_signal"] = macd_df[macds_col] if macds_col else float("nan")
        df["macd_hist"] = macd_df[macdh_col] if macdh_col else float("nan")
    else:
        df["macd_line"] = df["macd_signal"] = df["macd_hist"] = float("nan")

    df["roc_20"] = ta.roc(close, length=20)

    return df


# ---------------------------------------------------------------------------
# DB bulk insert
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT INTO indicators
    (time, ticker, interval,
     rsi_14, ma_50, ma_200, ema_20,
     bb_upper, bb_mid, bb_lower,
     atr_14, adx_14, volume_sma,
     macd_line, macd_signal, macd_hist, roc_20)
VALUES %s
ON CONFLICT (time, ticker, interval) DO NOTHING
"""

_CRITICAL = {"rsi_14", "ma_50", "ma_200", "atr_14", "adx_14"}


def _to_db_val(v):
    """Convert a value for DB insertion: NaN/None → None, else float."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _bulk_insert_ticker(ticker: str, df: pd.DataFrame, interval: str = "1d") -> int:
    """Bulk-insert all indicator rows for one ticker. Returns rows inserted."""
    import psycopg2.extras

    # Filter: only rows where all critical indicators are non-NaN
    mask = pd.Series(True, index=df.index)
    for col in _CRITICAL:
        if col in df.columns:
            mask &= df[col].notna()
    valid = df[mask]

    if valid.empty:
        return 0

    rows = []
    for idx, row in valid.iterrows():
        time_val = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        rows.append((
            time_val,
            ticker,
            interval,
            _to_db_val(row.get("rsi_14")),
            _to_db_val(row.get("ma_50")),
            _to_db_val(row.get("ma_200")),
            _to_db_val(row.get("ema_20")),
            _to_db_val(row.get("bb_upper")),
            _to_db_val(row.get("bb_mid")),
            _to_db_val(row.get("bb_lower")),
            _to_db_val(row.get("atr_14")),
            _to_db_val(row.get("adx_14")),
            _to_db_val(row.get("volume_sma")),
            _to_db_val(row.get("macd_line")),
            _to_db_val(row.get("macd_signal")),
            _to_db_val(row.get("macd_hist")),
            _to_db_val(row.get("roc_20")),
        ))

    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Insert in batches to keep memory bounded
            for i in range(0, len(rows), _BATCH_SIZE):
                batch = rows[i : i + _BATCH_SIZE]
                psycopg2.extras.execute_values(cur, _INSERT_SQL, batch, page_size=_BATCH_SIZE)
            inserted = len(rows)

    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_price_data(ticker: str) -> pd.DataFrame:
    """Load full OHLCV history for a single ticker from price_data."""
    from sqlalchemy import text

    engine = get_engine()
    query = text(
        """
        SELECT time, open, high, low, close, volume
        FROM price_data
        WHERE ticker = :ticker AND interval = '1d'
        ORDER BY time ASC
        """
    )
    df = pd.read_sql(query, engine, params={"ticker": ticker},
                      index_col="time", parse_dates=["time"])
    if not df.empty:
        df.index = pd.to_datetime(df.index, utc=True)
    return df


def hydrate(skip_existing: bool = True):
    """Backfill the indicators table for all active watchlist tickers.

    Args:
        skip_existing: If True, skip tickers that already have > 200 rows
                       in the indicators table (already hydrated).
    """
    tickers = get_watchlist(active_only=True)
    logger.info(f"Hydrating indicators for {len(tickers)} tickers")

    # Check which tickers are already hydrated
    already_done = set()
    if skip_existing:
        from sqlalchemy import text
        engine = get_engine()
        query = text(
            """
            SELECT ticker, COUNT(*) AS cnt
            FROM indicators
            WHERE interval = '1d'
            GROUP BY ticker
            HAVING COUNT(*) > 200
            """
        )
        with engine.connect() as conn:
            for row in conn.execute(query):
                already_done.add(row[0])

    to_process = [t for t in tickers if t not in already_done]
    if already_done:
        logger.info(f"Skipping {len(already_done)} already-hydrated tickers")
    logger.info(f"Processing {len(to_process)} tickers")

    total_rows = 0
    failed = []
    t0 = time.time()

    if _RICH:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        with progress:
            task = progress.add_task("[cyan]Hydrating indicators", total=len(to_process))
            for ticker in to_process:
                try:
                    df = _load_price_data(ticker)
                    if len(df) < 200:
                        logger.debug(f"{ticker}: only {len(df)} bars, skipping")
                        progress.advance(task)
                        continue

                    df = _compute_all_rows(df)
                    n = _bulk_insert_ticker(ticker, df)
                    total_rows += n
                    progress.update(task, description=f"[cyan]{ticker} ({n} rows)")
                except Exception as exc:
                    logger.warning(f"{ticker}: failed — {exc}")
                    failed.append(ticker)
                progress.advance(task)
    else:
        for i, ticker in enumerate(to_process):
            try:
                df = _load_price_data(ticker)
                if len(df) < 200:
                    logger.debug(f"{ticker}: only {len(df)} bars, skipping")
                    continue
                df = _compute_all_rows(df)
                n = _bulk_insert_ticker(ticker, df)
                total_rows += n
                if (i + 1) % 50 == 0:
                    logger.info(f"  Progress: {i+1}/{len(to_process)} tickers, {total_rows} rows")
            except Exception as exc:
                logger.warning(f"{ticker}: failed — {exc}")
                failed.append(ticker)

    elapsed = time.time() - t0
    logger.info(
        f"\n{'='*60}\n"
        f"Hydration complete\n"
        f"  Tickers processed: {len(to_process) - len(failed)}/{len(to_process)}\n"
        f"  Rows inserted:     {total_rows:,}\n"
        f"  Failed tickers:    {len(failed)}{' — ' + ', '.join(failed) if failed else ''}\n"
        f"  Time:              {elapsed:.1f}s ({elapsed/60:.1f}m)\n"
        f"{'='*60}"
    )

    return total_rows, failed


if __name__ == "__main__":
    hydrate()
