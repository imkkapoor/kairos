"""
strategies/indicators.py — Nightly technical indicator computation.

Computes RSI-14, SMA-50/200, EMA-20, Bollinger Bands (20,2), ATR-14, ADX-14,
Volume SMA-20 for all active watchlist tickers using the last 250 trading days
of OHLCV data already stored in price_data.

Key contract
------------
Two values are attached to every returned row_dict but are NOT written to the
indicators DB table — they live in memory only and are consumed by strategies:
  row_dict['close']  — latest close price  (strategies compare vs BB bands / MAs)
  row_dict['volume'] — latest volume value  (volume-confirmation multiplier)

These values live in price_data; no duplication in indicators is needed.
"""

import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import pandas_ta as ta
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

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from db.connection import (  # noqa: E402
    get_latest_price_bars,
    get_price_data,
    get_todays_indicators,
    get_watchlist,
    insert_indicator_row,
)

_MIN_ROWS   = 200   # need at least 200 bars to compute MA-200
_FETCH_DAYS = 420   # ~280 trading days — buffer above 250 to cover holidays


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_col(df: pd.DataFrame, prefix: str) -> str | None:
    """Return the first column whose name starts with *prefix*, or None."""
    for col in df.columns:
        if str(col).startswith(prefix):
            return col
    return None


def _is_nan(v) -> bool:
    """Return True if v is None or a floating-point NaN."""
    if v is None:
        return True
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def _compute_row(df: pd.DataFrame, ticker: str) -> dict | None:
    """Compute all indicators for the final row of *df*.

    Returns a dict with all indicator columns plus in-memory `close` and
    `volume` keys, or None if any required indicator value is NaN.
    """
    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # -- Compute --
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

    # -- Extract last-row scalar values --
    def last_val(s) -> float | None:
        """Return last element of a Series as float, or None if empty/None."""
        if s is None or (isinstance(s, pd.Series) and s.empty):
            return None
        return float(s.iloc[-1])

    rsi_val    = last_val(rsi)
    sma50_val  = last_val(sma50)
    sma200_val = last_val(sma200)
    ema20_val  = last_val(ema20)
    atr_val    = last_val(atr)
    vsma_val   = last_val(vsma)
    roc_val    = last_val(roc)

    # -- MACD (find columns by prefix — robust across minor versions) --
    macd_line_val = macd_signal_val = macd_hist_val = None
    if macd_df is not None and not macd_df.empty:
        macd_col = _find_col(macd_df, "MACD_")
        macds_col = _find_col(macd_df, "MACDs_")
        macdh_col = _find_col(macd_df, "MACDh_")
        if macd_col:
            macd_line_val = float(macd_df[macd_col].iloc[-1])
        if macds_col:
            macd_signal_val = float(macd_df[macds_col].iloc[-1])
        if macdh_col:
            macd_hist_val = float(macd_df[macdh_col].iloc[-1])

    # -- Bollinger Bands (find columns by prefix — robust across minor versions) --
    bb_upper = bb_mid = bb_lower = None
    if bb is not None and not bb.empty:
        bbu_col = _find_col(bb, "BBU")
        bbm_col = _find_col(bb, "BBM")
        bbl_col = _find_col(bb, "BBL")
        if bbu_col:
            bb_upper = float(bb[bbu_col].iloc[-1])
        if bbm_col:
            bb_mid   = float(bb[bbm_col].iloc[-1])
        if bbl_col:
            bb_lower = float(bb[bbl_col].iloc[-1])

    # -- ADX (find column by prefix) --
    adx_val = None
    if adx_df is not None and not adx_df.empty:
        adx_col = _find_col(adx_df, "ADX")
        if adx_col:
            adx_val = float(adx_df[adx_col].iloc[-1])

    values: dict = {
        "rsi_14":      rsi_val,
        "ma_50":       sma50_val,
        "ma_200":      sma200_val,
        "ema_20":      ema20_val,
        "bb_upper":    bb_upper,
        "bb_mid":      bb_mid,
        "bb_lower":    bb_lower,
        "atr_14":      atr_val,
        "adx_14":      adx_val,
        "volume_sma":  vsma_val,
        "macd_line":   macd_line_val,
        "macd_signal": macd_signal_val,
        "macd_hist":   macd_hist_val,
        "roc_20":      roc_val,
    }

    # Reject the row only if a *critical* indicator is NaN.
    # volume_sma NaN is tolerated — strategies default its multiplier to 1.0.
    _CRITICAL = {"rsi_14", "ma_50", "ma_200", "atr_14", "adx_14"}
    for k, v in values.items():
        if _is_nan(v):
            if k in _CRITICAL:
                logger.warning(f"[{ticker}] NaN in critical indicator '{k}' — skipping row")
                return None
            logger.debug(f"[{ticker}] NaN in non-critical indicator '{k}' — row will still be saved")

    return {
        "time":   df.index[-1],           # UTC-aware pandas Timestamp
        **values,
        "close":  float(close.iloc[-1]),  # in-memory only — NOT written to DB
        "volume": float(volume.iloc[-1]), # in-memory only — NOT written to DB
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_all(
    interval: str = "1d",
    for_date: Optional[datetime] = None,
) -> dict[str, dict]:
    """Compute and persist today's indicators for all active watchlist tickers.

    Returns {ticker: row_dict} where each row_dict contains all DB columns plus
    in-memory `close` and `volume` for use by downstream strategies.

    Incremental behaviour:
      - Tickers whose row already exists in the DB today are not recomputed;
        their latest close/volume are fetched via a single bulk query.
      - Only tickers missing today's row incur indicator computation + DB write.

    Skip conditions (logged as WARNING, no crash):
      - Fewer than 200 bars of price data (insufficient for MA-200)
      - Any required indicator evaluates to NaN after computation

    Parameters
    ----------
    for_date:
        When set, compute indicators using price data up to and including this
        date (historical replay). Defaults to the most recent bar (live mode).
    """
    tickers = get_watchlist(active_only=True)

    if for_date is not None:
        # Historical mode: pin data window to for_date
        _latest_bar_time = for_date.replace(hour=0, minute=0, second=0, microsecond=0)
        if _latest_bar_time.tzinfo is None:
            _latest_bar_time = _latest_bar_time.replace(tzinfo=timezone.utc)
        end_dt = _latest_bar_time + timedelta(days=1)
        start_dt = _latest_bar_time - timedelta(days=_FETCH_DAYS)
    else:
        # Live mode: determine the latest trading day from actual price data so
        # the cache lookup matches indicator timestamps (which inherit the price
        # bar time, e.g. 2026-03-25 04:00 UTC) rather than today's UTC date.
        _latest_bar_time = None
        try:
            _spy_bar = get_price_data("SPY", interval=interval, limit=1)
            if not _spy_bar.empty:
                _latest_bar_time = _spy_bar.index[-1]  # UTC-aware pd.Timestamp
        except Exception:
            pass
        end_dt = None
        start_dt = datetime.now(timezone.utc) - timedelta(days=_FETCH_DAYS)

    existing_today = get_todays_indicators(
        tickers, interval=interval, for_date=_latest_bar_time,
    )
    to_compute     = [t for t in tickers if t not in existing_today]

    # Bulk-fetch latest close/volume for already-computed tickers (single query)
    results: dict[str, dict] = {}
    if existing_today:
        latest_prices = get_latest_price_bars(list(existing_today.keys()), interval=interval)
        for ticker, ind_row in existing_today.items():
            row = dict(ind_row)
            prices = latest_prices.get(ticker, {})
            row["close"]  = prices.get("close")
            row["volume"] = prices.get("volume")
            results[ticker] = row

    # Compute fresh indicators for tickers not yet done today
    computed   = 0
    skipped    = 0

    def _process_ticker(ticker: str) -> None:
        nonlocal computed, skipped
        try:
            df = get_price_data(ticker, interval=interval, start=start_dt, end=end_dt)
            df = df.tail(250)

            if len(df) < _MIN_ROWS:
                logger.warning(
                    f"[{ticker}] only {len(df)} rows (min {_MIN_ROWS} required) — skipping"
                )
                skipped += 1
                return

            row = _compute_row(df, ticker)
            if row is None:
                skipped += 1
                return

            insert_indicator_row(ticker, interval, row)
            results[ticker] = row
            computed += 1

        except Exception as exc:
            logger.error(f"[{ticker}] indicator computation error: {exc}")
            skipped += 1

    if _RICH_AVAILABLE and to_compute:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Computing indicators...", total=len(to_compute))
            for ticker in to_compute:
                progress.update(task, description=f"[cyan]{ticker}[/cyan]")
                _process_ticker(ticker)
                progress.advance(task)
    else:
        for ticker in to_compute:
            _process_ticker(ticker)

    logger.info(
        f"compute_all: {computed} computed, {len(existing_today)} from DB cache, "
        f"{skipped} skipped — {len(results)} total ready"
    )
    return results
