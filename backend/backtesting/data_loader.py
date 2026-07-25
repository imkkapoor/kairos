"""
backtesting/data_loader.py — DB data loading and ROOS window generation.

Rules:
  - No yfinance calls — all data from DB via db/connection.py engine.
  - Returns dicts of DataFrames with UTC DatetimeIndex.
  - FX pair derived from PORTFOLIO_CURRENCY env var (default CAD → CADUSD).
"""

import os
from datetime import date, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from db.connection import get_engine

_PORTFOLIO_CURRENCY = os.environ.get("PORTFOLIO_CURRENCY", "CAD")


def _fx_pair_info() -> tuple[str, bool] | tuple[None, bool]:
    """Return (db_pair, invert) for converting native currency to USD.

    The DB stores USDCAD (1 USD = X CAD). For a CAD-currency portfolio the
    backtester needs CADUSD (1 CAD = X USD = 1/USDCAD), so invert=True.
    For a USD portfolio no conversion is needed — returns (None, False).
    """
    ccy = _PORTFOLIO_CURRENCY.upper()
    if ccy == "USD":
        return None, False
    # PORTFOLIO_CURRENCY=CAD: DB stores USDCAD; invert to get CADUSD
    return "USDCAD", True


# ---------------------------------------------------------------------------
# OHLCV loader
# ---------------------------------------------------------------------------

def load_ohlcv(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, pd.DataFrame]:
    """Load daily OHLCV for all tickers between start_date and end_date (inclusive).

    Returns {ticker: DataFrame} where each DataFrame has UTC DatetimeIndex
    and columns Open, High, Low, Close, Volume.
    Uses a single query for all tickers.
    """
    if not tickers:
        return {}

    engine = get_engine()
    query = text(
        """
        SELECT time, ticker, open, high, low, close, volume
        FROM price_data
        WHERE ticker = ANY(:tickers)
          AND interval = '1d'
          AND time >= :start_dt
          AND time <  :end_dt
        ORDER BY ticker, time ASC
        """
    )
    end_exclusive = end_date + timedelta(days=1)
    df = pd.read_sql(
        query,
        engine,
        params={"tickers": tickers, "start_dt": start_date, "end_dt": end_exclusive},
        parse_dates=["time"],
    )
    if df.empty:
        return {}

    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")

    result: dict[str, pd.DataFrame] = {}
    for ticker, group in df.groupby("ticker"):
        t = group[["open", "high", "low", "close", "volume"]].copy()
        t.columns = ["Open", "High", "Low", "Close", "Volume"]
        result[str(ticker)] = t

    return result


# ---------------------------------------------------------------------------
# Indicators loader
# ---------------------------------------------------------------------------

def load_indicators(
    tickers: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, pd.DataFrame]:
    """Load pre-computed indicator rows for all tickers between start_date and end_date.

    Returns {ticker: DataFrame} with UTC DatetimeIndex.
    Uses a single query for all tickers.
    """
    if not tickers:
        return {}

    engine = get_engine()
    query = text(
        """
        SELECT time, ticker,
               rsi_14, ma_50, ma_200, ema_20,
               bb_upper, bb_mid, bb_lower,
               atr_14, adx_14, volume_sma,
               macd_line, macd_signal, macd_hist, roc_20
        FROM indicators
        WHERE ticker = ANY(:tickers)
          AND interval = '1d'
          AND time >= :start_dt
          AND time <  :end_dt
        ORDER BY ticker, time ASC
        """
    )
    end_exclusive = end_date + timedelta(days=1)
    df = pd.read_sql(
        query,
        engine,
        params={"tickers": tickers, "start_dt": start_date, "end_dt": end_exclusive},
        parse_dates=["time"],
    )
    if df.empty:
        return {}

    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")

    result: dict[str, pd.DataFrame] = {}
    for ticker, group in df.groupby("ticker"):
        result[str(ticker)] = group.drop(columns=["ticker"], errors="ignore").copy()

    return result


# ---------------------------------------------------------------------------
# FX rates loader
# ---------------------------------------------------------------------------

def load_fx_rates(
    start_date: date,
    end_date: date,
) -> pd.Series | None:
    """Load FX rates for converting native currency positions to USD.

    Queries USDCAD from DB (1 USD = X CAD) and inverts to CADUSD (1 CAD = X USD)
    so the backtester can multiply native CAD prices directly by the returned rate
    to get USD values.

    Returns None for USD portfolios (PORTFOLIO_CURRENCY=USD) — caller uses fx=1.0.
    Returns a pd.Series indexed by UTC Timestamp (midnight) with forward-filled gaps.
    """
    pair, invert = _fx_pair_info()
    if pair is None:
        return None

    engine = get_engine()
    end_exclusive = end_date + timedelta(days=1)
    query = text(
        """
        SELECT time, rate
        FROM fx_rates
        WHERE pair = :pair
          AND time >= :start_dt
          AND time <  :end_dt
        ORDER BY time ASC
        """
    )
    df = pd.read_sql(
        query,
        engine,
        params={"pair": pair, "start_dt": start_date, "end_dt": end_exclusive},
        parse_dates=["time"],
    )
    if df.empty:
        return None

    df["time"] = pd.to_datetime(df["time"], utc=True).dt.normalize()
    df = df.set_index("time")["rate"]
    df = df.groupby(df.index).last()  # keep last rate per day

    if invert:
        df = 1.0 / df   # USDCAD → CADUSD (1 CAD = ? USD)

    # Reindex over full calendar range and forward-fill weekends / holidays
    full_range = pd.date_range(
        start=pd.Timestamp(start_date, tz="UTC"),
        end=pd.Timestamp(end_date, tz="UTC"),
        freq="D",
    )
    df = df.reindex(full_range).ffill().bfill()
    return df


# ---------------------------------------------------------------------------
# VIX loader
# ---------------------------------------------------------------------------

def load_vix(start_date: date, end_date: date) -> pd.Series:
    """Load VIX daily close from vix_data via db/connection.py.

    Returns a pd.Series indexed by UTC-midnight Timestamps with forward-fill
    applied over weekends / holidays — same pattern as load_fx_rates().

    If no VIX data exists in the range, logs a warning and returns an empty
    Series. The backtester defaults to NORMAL (fail-open) for all days.
    """
    from db.connection import get_vix_range
    from loguru import logger

    series = get_vix_range(start_date, end_date)
    if series.empty:
        logger.warning(
            f"load_vix: no VIX data found for {start_date} → {end_date}. "
            "Backtester will default to NORMAL regime for all days. "
            "Run 'make fetch-vix' to backfill VIX history."
        )
    return series


# ---------------------------------------------------------------------------

def generate_roos_windows(
    start: date,
    end: date,
    train_years: int = 2,
    test_months: int = 6,
) -> list[dict]:
    """Generate all non-overlapping out-of-sample ROOS windows.

    Each window dict:
        train_start, train_end, test_start, test_end (all date objects)
        window_index (1-based int)

    The test windows are independent (no data leakage) and contiguous.
    Training window is 2 years immediately before each test window.
    Step size equals test_months — the test windows never overlap.
    """
    windows = []
    window_index = 1

    # First test window begins after the first training period
    test_start = start + relativedelta(years=train_years)

    while True:
        test_end = test_start + relativedelta(months=test_months)
        if test_end > end:
            break

        windows.append(
            {
                "train_start":  test_start - relativedelta(years=train_years),
                "train_end":    test_start,
                "test_start":   test_start,
                "test_end":     test_end,
                "window_index": window_index,
            }
        )

        test_start = test_end   # advance by one test step (no overlap)
        window_index += 1

    return windows
