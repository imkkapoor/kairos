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

import pandas as pd
import pandas_ta as ta
from loguru import logger

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
        "rsi_14":    rsi_val,
        "ma_50":     sma50_val,
        "ma_200":    sma200_val,
        "ema_20":    ema20_val,
        "bb_upper":  bb_upper,
        "bb_mid":    bb_mid,
        "bb_lower":  bb_lower,
        "atr_14":    atr_val,
        "adx_14":    adx_val,
        "volume_sma": vsma_val,
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

def compute_all(interval: str = "1d") -> dict[str, dict]:
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
    """
    tickers = get_watchlist(active_only=True)
    existing_today = get_todays_indicators(tickers, interval=interval)
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
    start_dt   = datetime.now(timezone.utc) - timedelta(days=_FETCH_DAYS)
    computed   = 0
    skipped    = 0

    for ticker in to_compute:
        try:
            df = get_price_data(ticker, interval=interval, start=start_dt)
            df = df.tail(250)

            if len(df) < _MIN_ROWS:
                logger.warning(
                    f"[{ticker}] only {len(df)} rows (min {_MIN_ROWS} required) — skipping"
                )
                skipped += 1
                continue

            row = _compute_row(df, ticker)
            if row is None:
                skipped += 1
                continue

            insert_indicator_row(ticker, interval, row)
            results[ticker] = row
            computed += 1

        except Exception as exc:
            logger.error(f"[{ticker}] indicator computation error: {exc}")
            skipped += 1

    logger.info(
        f"compute_all: {computed} computed, {len(existing_today)} from DB cache, "
        f"{skipped} skipped — {len(results)} total ready"
    )
    return results
