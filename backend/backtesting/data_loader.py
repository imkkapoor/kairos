"""
backtesting/data_loader.py — Load historical data from Kairos DB for backtesting.

ALL data comes from the DB. No yfinance or external API calls.

Indicators are computed from OHLCV using pandas_ta (same formulas as
strategies/indicators.py) since the indicators DB table only stores
recent scheduler-computed values, not full history.
"""

import sys
import os

import pandas as pd
import pandas_ta as ta
from sqlalchemy import text

# Ensure backend/ is on sys.path so db.connection can be imported
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from db.connection import get_engine


def _find_col(df: pd.DataFrame, prefix: str) -> str | None:
    """Return the first column whose name starts with prefix, or None."""
    for col in df.columns:
        if str(col).startswith(prefix):
            return col
    return None


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators from OHLCV data.

    Uses the same pandas_ta formulas as strategies/indicators.py to ensure
    consistency between live and backtested signals.
    """
    close = df["close"].astype(float) if "close" in df.columns else df["Close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else df["High"].astype(float)
    low = df["low"].astype(float) if "low" in df.columns else df["Low"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else df["Volume"].astype(float)

    # RSI-14
    df["rsi_14"] = ta.rsi(close, length=14)

    # SMA-50, SMA-200
    df["ma_50"] = ta.sma(close, length=50)
    df["ma_200"] = ta.sma(close, length=200)

    # EMA-20
    df["ema_20"] = ta.ema(close, length=20)

    # Bollinger Bands (20, 2)
    bb = ta.bbands(close, length=20, std=2.0)
    if bb is not None and not bb.empty:
        bbu = _find_col(bb, "BBU_")
        bbm = _find_col(bb, "BBM_")
        bbl = _find_col(bb, "BBL_")
        df["bb_upper"] = bb[bbu] if bbu else float("nan")
        df["bb_mid"] = bb[bbm] if bbm else float("nan")
        df["bb_lower"] = bb[bbl] if bbl else float("nan")
    else:
        df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = float("nan")

    # ATR-14
    df["atr_14"] = ta.atr(high, low, close, length=14)

    # ADX-14
    adx_df = ta.adx(high, low, close, length=14)
    if adx_df is not None and not adx_df.empty:
        adx_col = _find_col(adx_df, "ADX_")
        df["adx_14"] = adx_df[adx_col] if adx_col else float("nan")
    else:
        df["adx_14"] = float("nan")

    # Volume SMA-20
    df["volume_sma"] = ta.sma(volume, length=20)

    # MACD (12, 26, 9)
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

    # ROC-20
    df["roc_20"] = ta.roc(close, length=20)

    return df


def load_price_data(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Load OHLCV price data for a ticker from the DB.

    Returns a UTC-indexed DataFrame with columns: open, high, low, close, volume.
    Index name is 'time'.
    """
    engine = get_engine()
    query = text(
        """
        SELECT time, open, high, low, close, volume
        FROM price_data
        WHERE ticker = :ticker AND interval = '1d'
          AND time >= :start_date AND time <= :end_date
        ORDER BY time ASC
        """
    )
    df = pd.read_sql(
        query, engine,
        params={"ticker": ticker, "start_date": start_date, "end_date": end_date},
        index_col="time",
        parse_dates=["time"],
    )
    if not df.empty:
        df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "time"
    return df


def load_combined(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Load OHLCV + compute indicators for a ticker.

    Loads extra history before start_date (250 bars) so indicators like MA-200
    are warm by the time the actual backtest period begins. Then trims to the
    requested range.

    Returns a single DataFrame with columns:
      Open, High, Low, Close, Volume (capitalized for backtesting.py)
      + all indicator columns (rsi_14, ma_50, etc.)
    """
    engine = get_engine()

    # Load extra history for indicator warmup (250 trading days ~ 365 calendar days)
    query = text(
        """
        SELECT time, open, high, low, close, volume
        FROM price_data
        WHERE ticker = :ticker AND interval = '1d'
          AND time <= :end_date
        ORDER BY time ASC
        """
    )
    df = pd.read_sql(
        query, engine,
        params={"ticker": ticker, "end_date": end_date},
        index_col="time",
        parse_dates=["time"],
    )

    if df.empty or len(df) < 30:
        return pd.DataFrame()

    if not df.empty:
        df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "time"

    # Compute indicators on full history
    df = _compute_indicators(df)

    # Trim to requested date range (indicators are now warm)
    df = df[df.index >= pd.Timestamp(start_date, tz="UTC")]

    # Reject tickers that don't have enough bars in the actual period.
    # Tickers listed post-2020 (spinoffs, IPOs, etc.) won't have meaningful
    # in-sample history and would skew averages with very few trades.
    if len(df) < 100:
        return pd.DataFrame()

    # backtesting.py requires capitalized OHLCV column names
    df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }, inplace=True)

    return df


def load_ticker_list(min_rows: int = 100, start_date: str | None = None) -> list[str]:
    """Return active watchlist tickers that have >= min_rows in price_data.

    If start_date is given, counts only rows on or after that date so tickers
    with insufficient history for the target period are excluded upfront.
    """
    engine = get_engine()
    if start_date:
        query = text(
            """
            SELECT p.ticker
            FROM price_data p
            JOIN watchlist w ON w.ticker = p.ticker AND w.active = TRUE
            WHERE p.interval = '1d' AND p.time >= :start_date
            GROUP BY p.ticker
            HAVING COUNT(*) >= :min_rows
            ORDER BY p.ticker
            """
        )
        with engine.connect() as conn:
            rows = conn.execute(query, {"min_rows": min_rows, "start_date": start_date}).fetchall()
    else:
        query = text(
            """
            SELECT p.ticker
            FROM price_data p
            JOIN watchlist w ON w.ticker = p.ticker AND w.active = TRUE
            WHERE p.interval = '1d'
            GROUP BY p.ticker
            HAVING COUNT(*) >= :min_rows
            ORDER BY p.ticker
            """
        )
        with engine.connect() as conn:
            rows = conn.execute(query, {"min_rows": min_rows}).fetchall()
    return [row[0] for row in rows]
