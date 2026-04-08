"""
data/hydrate_indicators.py — Backfill the indicators table with full historical data.

For each ticker in the watchlist, loads ALL available price_data, computes all
technical indicators using vectorized pandas_ta on the full series, then bulk-inserts
every valid bar into the indicators table.

Idempotent — ON CONFLICT DO NOTHING — safe to interrupt and resume at any time.

Usage
-----
    # Backfill ALL tickers (default):
    cd backend && python -m data.hydrate_indicators

    # Single ticker (re-run or new arrival):
    cd backend && python -m data.hydrate_indicators --ticker RY.TO

    # Ticker list from a file (one ticker per line):
    cd backend && python -m data.hydrate_indicators --file tickers.txt

    # Force overwrite existing rows (uses ON CONFLICT DO UPDATE):
    cd backend && python -m data.hydrate_indicators --force

Why this script exists
----------------------
  `strategies/indicators.py:compute_all()` is designed for *daily* incremental use —
  it only writes today's single indicator bar per ticker.  This script writes the
  FULL history needed by the Walk-Forward Analysis backtester (Phase 4).

  Fresh setup order:
    make setup             (seeds watchlist + backfills OHLCV via yfinance)
    make hydrate-fx        (backfills USDCAD FX rates)
    make hydrate-indicators (this script — fills in all historical indicators)
    make backtest          (Phase 4 WFA)

Min bars required
-----------------
  MA-200 needs at least 200 bars.  Bars before that threshold are skipped
  automatically (pandas_ta emits NaN for early rows).
"""

import argparse
import math
import os
import sys
import time as _time_module
from pathlib import Path

# Ensure backend/ is on sys.path
_BACKEND_DIR = Path(__file__).parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta
import psycopg2.extras
from dotenv import load_dotenv
from loguru import logger

load_dotenv(_BACKEND_DIR / ".env")

from db.connection import get_conn, get_engine, get_watchlist, get_price_data

try:
    from rich.console import Console
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

_INTERVAL = "1d"
_BATCH_SIZE = 500  # rows per psycopg2 execute_values call


# ---------------------------------------------------------------------------
# Core computation (vectorized — one call computes the full history)
# ---------------------------------------------------------------------------

def _find_col(df: pd.DataFrame, prefix: str) -> str | None:
    for col in df.columns:
        if str(col).startswith(prefix):
            return col
    return None


def _compute_all_rows(df: pd.DataFrame, ticker: str) -> list[dict]:
    """Compute indicators for every bar in df using vectorized pandas_ta.

    Returns a list of row dicts — one per calendar bar — skipping rows where
    any *critical* indicator is NaN (insufficient history).

    Critical indicators (all must be present for a row to be written):
        rsi_14, ma_50, ma_200, atr_14, adx_14

    Non-critical (written as NULL when NaN):
        ema_20, bb_upper/mid/lower, volume_sma, macd_*, roc_20
    """
    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    rsi    = ta.rsi(close, length=14)
    sma50  = ta.sma(close, length=50)
    sma200 = ta.sma(close, length=200)
    ema20  = ta.ema(close, length=20)
    bb     = ta.bbands(close, length=20, std=2.0)
    atr    = ta.atr(high, low, close, length=14)
    adx_df = ta.adx(high, low, close, length=14)
    vsma   = ta.sma(volume, length=20)
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    roc    = ta.roc(close, length=20)

    # Extract named columns (pandas_ta naming varies by version)
    bb_upper_s = bb[_find_col(bb, "BBU")] if (bb is not None and _find_col(bb, "BBU")) else None
    bb_mid_s   = bb[_find_col(bb, "BBM")] if (bb is not None and _find_col(bb, "BBM")) else None
    bb_lower_s = bb[_find_col(bb, "BBL")] if (bb is not None and _find_col(bb, "BBL")) else None

    macd_line_s = macd_signal_s = macd_hist_s = None
    if macd_df is not None and not macd_df.empty:
        c = _find_col(macd_df, "MACD_");  macd_line_s   = macd_df[c]  if c else None
        c = _find_col(macd_df, "MACDs_"); macd_signal_s = macd_df[c]  if c else None
        c = _find_col(macd_df, "MACDh_"); macd_hist_s   = macd_df[c]  if c else None

    adx_s = None
    if adx_df is not None and not adx_df.empty:
        c = _find_col(adx_df, "ADX_"); adx_s = adx_df[c] if c else None

    # Build a combined DataFrame aligned on df.index
    combined = pd.DataFrame(index=df.index)
    combined["rsi_14"]      = rsi
    combined["ma_50"]       = sma50
    combined["ma_200"]      = sma200
    combined["ema_20"]      = ema20
    combined["bb_upper"]    = bb_upper_s if bb_upper_s is not None else float("nan")
    combined["bb_mid"]      = bb_mid_s   if bb_mid_s   is not None else float("nan")
    combined["bb_lower"]    = bb_lower_s if bb_lower_s is not None else float("nan")
    combined["atr_14"]      = atr
    combined["adx_14"]      = adx_s      if adx_s is not None else float("nan")
    combined["volume_sma"]  = vsma
    combined["macd_line"]   = macd_line_s   if macd_line_s   is not None else float("nan")
    combined["macd_signal"] = macd_signal_s if macd_signal_s is not None else float("nan")
    combined["macd_hist"]   = macd_hist_s   if macd_hist_s   is not None else float("nan")
    combined["roc_20"]      = roc

    _CRITICAL = {"rsi_14", "ma_50", "ma_200", "atr_14", "adx_14"}

    rows: list[dict] = []
    for ts, row in combined.iterrows():
        # Skip rows where any critical indicator is NaN (early history)
        skip = False
        for col in _CRITICAL:
            v = row[col]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                skip = True
                break
        if skip:
            continue

        def _safe(v):
            if v is None:
                return None
            try:
                f = float(v)
                return None if math.isnan(f) else f
            except (TypeError, ValueError):
                return None

        rows.append({
            "time":        ts.to_pydatetime(),
            "rsi_14":      _safe(row["rsi_14"]),
            "ma_50":       _safe(row["ma_50"]),
            "ma_200":      _safe(row["ma_200"]),
            "ema_20":      _safe(row["ema_20"]),
            "bb_upper":    _safe(row["bb_upper"]),
            "bb_mid":      _safe(row["bb_mid"]),
            "bb_lower":    _safe(row["bb_lower"]),
            "atr_14":      _safe(row["atr_14"]),
            "adx_14":      _safe(row["adx_14"]),
            "volume_sma":  _safe(row["volume_sma"]),
            "macd_line":   _safe(row["macd_line"]),
            "macd_signal": _safe(row["macd_signal"]),
            "macd_hist":   _safe(row["macd_hist"]),
            "roc_20":      _safe(row["roc_20"]),
        })

    return rows


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def _insert_rows(ticker: str, rows: list[dict], force: bool = False) -> int:
    """Bulk-insert indicator rows. Returns count actually inserted."""
    if not rows:
        return 0

    tuples = [
        (
            r["time"], ticker, _INTERVAL,
            r["rsi_14"], r["ma_50"], r["ma_200"], r["ema_20"],
            r["bb_upper"], r["bb_mid"], r["bb_lower"],
            r["atr_14"], r["adx_14"], r["volume_sma"],
            r["macd_line"], r["macd_signal"], r["macd_hist"], r["roc_20"],
        )
        for r in rows
    ]

    conflict = (
        "DO UPDATE SET "
        "rsi_14=EXCLUDED.rsi_14, ma_50=EXCLUDED.ma_50, ma_200=EXCLUDED.ma_200, "
        "ema_20=EXCLUDED.ema_20, bb_upper=EXCLUDED.bb_upper, bb_mid=EXCLUDED.bb_mid, "
        "bb_lower=EXCLUDED.bb_lower, atr_14=EXCLUDED.atr_14, adx_14=EXCLUDED.adx_14, "
        "volume_sma=EXCLUDED.volume_sma, macd_line=EXCLUDED.macd_line, "
        "macd_signal=EXCLUDED.macd_signal, macd_hist=EXCLUDED.macd_hist, roc_20=EXCLUDED.roc_20"
        if force else "DO NOTHING"
    )

    sql = f"""
        INSERT INTO indicators
            (time, ticker, interval,
             rsi_14, ma_50, ma_200, ema_20,
             bb_upper, bb_mid, bb_lower,
             atr_14, adx_14, volume_sma,
             macd_line, macd_signal, macd_hist, roc_20)
        VALUES %s
        ON CONFLICT (time, ticker, interval) {conflict}
        RETURNING time
    """

    inserted = 0
    for i in range(0, len(tuples), _BATCH_SIZE):
        batch = tuples[i : i + _BATCH_SIZE]
        with get_conn() as conn:
            with conn.cursor() as cur:
                result = psycopg2.extras.execute_values(
                    cur, sql, batch, fetch=True
                )
                inserted += len(result)

    return inserted


# ---------------------------------------------------------------------------
# Per-ticker orchestrator
# ---------------------------------------------------------------------------

def hydrate_ticker(ticker: str, force: bool = False) -> tuple[int, int]:
    """Load full price history for ticker, compute + insert all indicator rows.

    Returns (rows_computed, rows_inserted).
    """
    try:
        df = get_price_data(ticker, interval=_INTERVAL)
        if df.empty:
            logger.warning(f"[{ticker}] No price data found — skipping")
            return 0, 0

        rows = _compute_all_rows(df, ticker)
        if not rows:
            logger.warning(f"[{ticker}] No valid indicator rows computed — skipping")
            return 0, 0

        inserted = _insert_rows(ticker, rows, force=force)
        return len(rows), inserted

    except Exception as exc:
        logger.error(f"[{ticker}] Error during hydration: {exc}")
        return 0, 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hydrate full historical indicators for backtesting (Phase 4)"
    )
    parser.add_argument("--ticker", metavar="SYM", help="Process a single ticker only")
    parser.add_argument("--file", metavar="PATH", help="File with one ticker per line")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing rows (ON CONFLICT DO UPDATE). Default: skip existing.",
    )
    args = parser.parse_args()

    logger.info("=== Kairos indicator hydration starting ===")

    if args.ticker:
        tickers = [args.ticker.upper()]
    elif args.file:
        p = Path(args.file)
        if not p.exists():
            print(f"ERROR: file not found: {args.file}")
            sys.exit(1)
        tickers = [t.strip().upper() for t in p.read_text().splitlines() if t.strip()]
    else:
        tickers = get_watchlist(active_only=True)

    n = len(tickers)
    mode = "OVERWRITE" if args.force else "SKIP EXISTING"
    print(f"\nKairos indicator hydration — {n} tickers | mode: {mode}\n")

    start_wall = _time_module.time()
    total_computed = total_inserted = skipped = 0

    if _RICH and n > 1:
        con = Console()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=con,
        ) as progress:
            task = progress.add_task("Hydrating…", total=n)
            for ticker in tickers:
                progress.update(task, description=f"[cyan]{ticker:<10}[/cyan]")
                computed, inserted = hydrate_ticker(ticker, force=args.force)
                if computed == 0:
                    skipped += 1
                else:
                    total_computed += computed
                    total_inserted += inserted
                progress.advance(task)
    else:
        for i, ticker in enumerate(tickers, 1):
            print(f"  [{i:>4}/{n}] {ticker}…", end=" ", flush=True)
            computed, inserted = hydrate_ticker(ticker, force=args.force)
            if computed == 0:
                skipped += 1
                print("skipped")
            else:
                total_computed += computed
                total_inserted += inserted
                print(f"{computed} rows computed, {inserted} inserted")

    elapsed = _time_module.time() - start_wall
    print(f"\n{'='*55}")
    print(f"  Tickers processed:  {n - skipped} / {n}")
    print(f"  Indicator rows:     {total_computed:,} computed | {total_inserted:,} inserted")
    print(f"  Skipped tickers:    {skipped}")
    print(f"  Wall time:          {elapsed/60:.1f} min")
    print(f"  Mode:               {mode}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
