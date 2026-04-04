"""
db/connection.py — The ONLY file in Kairos that reads from or writes to the database.

All other modules must import and call the public functions defined here.
No other file may import psycopg2 or sqlalchemy directly.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Load .env from backend/.env (two directories up from this file: backend/db/ → backend/)
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)


# ---------------------------------------------------------------------------
# Timezone utility
# ---------------------------------------------------------------------------

def to_utc(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to UTC.

    Raises ValueError if a naive datetime (no tzinfo) is passed — naive datetimes
    are forbidden throughout Kairos to prevent silent timezone bugs.
    """
    if dt.tzinfo is None:
        raise ValueError(
            f"Naive datetime passed to to_utc(): {dt!r}. "
            "All datetimes in Kairos must be timezone-aware. "
            "Use datetime.now(timezone.utc) instead of datetime.now()."
        )
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dsn() -> str:
    """Build a psycopg2 DSN string from environment variables."""
    return (
        f"host={os.environ['DB_HOST']} "
        f"port={os.environ['DB_PORT']} "
        f"dbname={os.environ['DB_NAME']} "
        f"user={os.environ['DB_USER']} "
        f"password={os.environ['DB_PASSWORD']}"
    )


def _db_url() -> str:
    """Build a SQLAlchemy connection URL from environment variables."""
    host = os.environ["DB_HOST"]
    port = os.environ["DB_PORT"]
    name = os.environ["DB_NAME"]
    user = os.environ["DB_USER"]
    pw   = os.environ["DB_PASSWORD"]
    return f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{name}"


# ---------------------------------------------------------------------------
# Core connection primitives
# ---------------------------------------------------------------------------

def ping() -> bool:
    """Return True if the database is reachable, False otherwise.

    Safe to call repeatedly — does not raise.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a cached SQLAlchemy engine (one instance per process).

    pool_pre_ping=True verifies connections before use, preventing stale-connection errors
    after long idle periods.
    """
    return create_engine(_db_url(), pool_pre_ping=True)


@contextmanager
def get_conn():
    """Context manager that yields a psycopg2 connection.

    Commits on clean exit, rolls back on any exception, and always closes the connection.

    Usage::

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ...")
    """
    conn = psycopg2.connect(_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist(active_only: bool = True) -> list[str]:
    """Return all ticker symbols from the watchlist, sorted alphabetically.

    Parameters
    ----------
    active_only:
        When True (default), only return tickers where active = TRUE.
    """
    engine = get_engine()
    query = "SELECT ticker FROM watchlist"
    if active_only:
        query += " WHERE active = TRUE"
    query += " ORDER BY ticker"
    with engine.connect() as conn:
        result = conn.execute(text(query))
        return [row[0] for row in result]


def add_to_watchlist(
    ticker: str,
    name: Optional[str] = None,
    sector: Optional[str] = None,
    market: str = "US",
    notes: Optional[str] = None,
) -> None:
    """Insert a single ticker into the watchlist.

    Silently does nothing if the ticker already exists.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchlist (ticker, name, sector, market, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ticker) DO NOTHING
                """,
                (ticker, name, sector, market, notes),
            )


def seed_watchlist(tickers: list[dict]) -> None:
    """Bulk upsert a list of tickers into the watchlist.

    Each dict must contain 'ticker'. Optional keys: 'name', 'sector', 'market', 'notes'.
    On conflict (ticker already exists), sets active = TRUE to re-activate any previously
    deactivated entries.

    Parameters
    ----------
    tickers:
        List of dicts, e.g. [{"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology", "market": "US"}]
    """
    if not tickers:
        return

    rows = [
        (
            t["ticker"],
            t.get("name"),
            t.get("sector"),
            t.get("market", "US"),
            t.get("notes"),
        )
        for t in tickers
    ]

    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO watchlist (ticker, name, sector, market, notes)
                VALUES %s
                ON CONFLICT (ticker) DO UPDATE SET active = TRUE
                """,
                rows,
            )


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def insert_price_data(df: pd.DataFrame, ticker: str, interval: str = "1d") -> int:
    """Bulk insert OHLCV rows for a ticker. Duplicate rows are silently skipped.

    Parameters
    ----------
    df:
        DataFrame with a UTC-aware DatetimeIndex (or a 'time' column) and columns:
        open, high, low, close, volume. All timestamps MUST be UTC-aware.
    ticker:
        The ticker symbol (e.g. "AAPL").
    interval:
        The bar interval (e.g. "1d", "1h"). Defaults to "1d".

    Returns
    -------
    int
        Number of rows actually inserted (conflicts excluded).

    Raises
    ------
    ValueError
        If the time column/index is not UTC-aware.
    """
    if df.empty:
        return 0

    df = df.copy()
    if "time" not in df.columns:
        df["time"] = df.index
    df["time"] = pd.to_datetime(df["time"], utc=True)

    if df["time"].dt.tz is None:
        raise ValueError(
            f"insert_price_data: timestamps for {ticker} are not UTC-aware. "
            "Convert with df.index.tz_convert('UTC') before calling this function."
        )

    rows = [
        (
            row["time"].to_pydatetime(),
            ticker,
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            int(row["volume"]),
            interval,
            "yfinance",
        )
        for _, row in df.iterrows()
    ]

    if not rows:
        return 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            result = psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO price_data
                    (time, ticker, open, high, low, close, volume, interval, source)
                VALUES %s
                ON CONFLICT (time, ticker, interval) DO NOTHING
                RETURNING time
                """,
                rows,
                fetch=True,
            )
            return len(result)


def get_price_data(
    ticker: str,
    interval: str = "1d",
    limit: Optional[int] = None,
    start: Optional[datetime] = None,
) -> pd.DataFrame:
    """Return OHLCV data for a ticker as a UTC-indexed DataFrame.

    Parameters
    ----------
    ticker:
        The ticker symbol.
    interval:
        Bar interval filter (default "1d").
    limit:
        If set, return only the most recent N rows.
    start:
        If set, only return rows at or after this UTC-aware datetime.

    Returns
    -------
    pd.DataFrame
        Indexed by UTC-aware 'time', with columns: open, high, low, close, volume,
        interval, source, ticker.
    """
    engine = get_engine()
    conditions = ["ticker = :ticker", "interval = :interval"]
    params: dict = {"ticker": ticker, "interval": interval}

    if start is not None:
        start = to_utc(start)
        conditions.append("time >= :start")
        params["start"] = start

    where = " AND ".join(conditions)
    if limit:
        # Wrap in subquery so we get the most-recent N rows, returned in ASC order
        inner = f"SELECT * FROM price_data WHERE {where} ORDER BY time DESC LIMIT {int(limit)}"
        query = text(f"SELECT * FROM ({inner}) sub ORDER BY time ASC")
    else:
        query = text(f"SELECT * FROM price_data WHERE {where} ORDER BY time ASC")

    df = pd.read_sql(query, engine, params=params, index_col="time", parse_dates=["time"])
    if not df.empty:
        df.index = pd.to_datetime(df.index, utc=True)
    return df


def get_latest_timestamp(ticker: str, interval: str = "1d") -> Optional[pd.Timestamp]:
    """Return the most recent stored UTC timestamp for a ticker/interval pair.

    Returns None if no data exists for the given ticker and interval.
    """
    engine = get_engine()
    query = text(
        "SELECT MAX(time) FROM price_data WHERE ticker = :ticker AND interval = :interval"
    )
    with engine.connect() as conn:
        val = conn.execute(query, {"ticker": ticker, "interval": interval}).scalar()

    if val is None:
        return None

    ts = pd.Timestamp(val)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


# ---------------------------------------------------------------------------
# Fetch audit log
# ---------------------------------------------------------------------------

def log_daily_fetch(
    tickers_total: int,
    tickers_success: int,
    tickers_skipped: int,
    tickers_failed: int,
    failed_tickers: list,
    rows_inserted: int,
    duration_secs: float,
    notes: Optional[str] = None,
) -> None:
    """Insert one summary row into fetch_log."""
    failed_str = ",".join(failed_tickers) if failed_tickers else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fetch_log
                    (tickers_total, tickers_success, tickers_skipped,
                     tickers_failed, failed_tickers, rows_inserted, duration_secs, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (tickers_total, tickers_success, tickers_skipped,
                 tickers_failed, failed_str, rows_inserted, duration_secs, notes),
            )


def get_fetch_history(days: int = 30) -> pd.DataFrame:
    """Return the last *days* rows from fetch_log, ordered by fetch_time DESC."""
    engine = get_engine()
    query = text(
        "SELECT * FROM fetch_log ORDER BY fetch_time DESC LIMIT :days"
    )
    return pd.read_sql(query, engine, params={"days": days}, parse_dates=["fetch_time"])


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

def save_portfolio_snapshot(
    cash: float,
    total_value: float,
    positions: dict,
    daily_pnl: Optional[float] = None,
    total_pnl: Optional[float] = None,
    drawdown: Optional[float] = None,
    currency: str = "CAD",
) -> None:
    """Persist a point-in-time portfolio snapshot.

    Parameters
    ----------
    cash:
        Current cash balance in portfolio base currency.
    total_value:
        Total portfolio value (cash + open positions) in portfolio base currency.
    positions:
        JSON-serialisable dict mapping ticker → position details.
    daily_pnl:
        Profit/loss for the current trading day (can be None).
    total_pnl:
        Cumulative profit/loss since inception (can be None).
    drawdown:
        Current drawdown as a fraction of peak value (can be None).
    currency:
        Portfolio base currency code (default 'CAD').
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO portfolio_snapshots
                    (cash, total_value, positions, daily_pnl, total_pnl, drawdown, currency)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cash,
                    total_value,
                    json.dumps(positions),
                    daily_pnl,
                    total_pnl,
                    drawdown,
                    currency,
                ),
            )


def get_latest_snapshot() -> Optional[dict]:
    """Return the most recent portfolio snapshot as a plain dict.

    Returns None if no snapshot has ever been saved.
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM portfolio_snapshots ORDER BY time DESC LIMIT 1")
        ).mappings().first()

    return dict(row) if row is not None else None


def get_portfolio_history(days: int = 90) -> pd.DataFrame:
    """Return the last N days of portfolio snapshots as a UTC-indexed DataFrame.

    Parameters
    ----------
    days:
        How many calendar days of history to return (default 90).

    Returns
    -------
    pd.DataFrame
        Indexed by UTC-aware 'time', with columns matching the portfolio_snapshots table.
    """
    engine = get_engine()
    query = text(
        f"""
        SELECT * FROM portfolio_snapshots
        WHERE time >= NOW() - INTERVAL '{int(days)} days'
        ORDER BY time ASC
        """
    )
    df = pd.read_sql(query, engine, index_col="time", parse_dates=["time"])
    if not df.empty:
        df.index = pd.to_datetime(df.index, utc=True)
    return df


# ---------------------------------------------------------------------------
# FX rates
# ---------------------------------------------------------------------------

def insert_fx_rate(
    pair: str,
    rate: float,
    at_time: Optional[datetime] = None,
    source: str = "yfinance",
) -> None:
    """Upsert an FX rate row into the fx_rates table.

    Parameters
    ----------
    pair:
        Currency pair, e.g. 'USDCAD' meaning 1 USD = rate CAD.
    rate:
        The exchange rate.
    at_time:
        UTC-aware datetime for this rate; defaults to now().
    source:
        Data source label (default 'yfinance').
    """
    if at_time is None:
        at_time = datetime.now(timezone.utc)
    at_time = to_utc(at_time)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fx_rates (time, pair, rate, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT ON CONSTRAINT fx_rates_unique DO UPDATE SET rate = EXCLUDED.rate
                """,
                (at_time, pair, round(float(rate), 6), source),
            )


def get_fx_rate(
    pair: str,
    at_time: Optional[datetime] = None,
    since: Optional[datetime] = None,
) -> Optional[float]:
    """Return the most recent stored FX rate for *pair* at or before *at_time*.

    Parameters
    ----------
    pair:
        Currency pair, e.g. 'USDCAD'.
    at_time:
        UTC-aware datetime upper bound; defaults to now().
    since:
        Optional UTC-aware lower bound. When provided, only rows with
        ``time >= since`` are considered. Use this to check whether a rate
        has already been stored *today* (pass midnight UTC as *since*).

    Returns None if no row is found.
    """
    if at_time is None:
        at_time = datetime.now(timezone.utc)
    at_time = to_utc(at_time)
    if since is not None:
        since = to_utc(since)
        query = text(
            """
            SELECT rate FROM fx_rates
            WHERE pair = :pair AND time <= :at_time AND time >= :since
            ORDER BY time DESC
            LIMIT 1
            """
        )
        params: dict = {"pair": pair, "at_time": at_time, "since": since}
    else:
        query = text(
            """
            SELECT rate FROM fx_rates
            WHERE pair = :pair AND time <= :at_time
            ORDER BY time DESC
            LIMIT 1
            """
        )
        params = {"pair": pair, "at_time": at_time}
    engine = get_engine()
    with engine.connect() as conn:
        val = conn.execute(query, params).scalar()
    return float(val) if val is not None else None


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def insert_indicator_row(ticker: str, interval: str, row: dict) -> None:
    """Insert a single computed indicator row. ON CONFLICT DO NOTHING (idempotent).

    The row dict may contain extra in-memory keys (close, volume) — they are
    silently ignored; only DB columns are written.
    """
    time_val = row["time"]
    if hasattr(time_val, "to_pydatetime"):
        time_val = time_val.to_pydatetime()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO indicators
                    (time, ticker, interval,
                     rsi_14, ma_50, ma_200, ema_20,
                     bb_upper, bb_mid, bb_lower,
                     atr_14, adx_14, volume_sma,
                     macd_line, macd_signal, macd_hist, roc_20)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (time, ticker, interval) DO NOTHING
                """,
                (
                    time_val,
                    ticker,
                    interval,
                    row.get("rsi_14"),
                    row.get("ma_50"),
                    row.get("ma_200"),
                    row.get("ema_20"),
                    row.get("bb_upper"),
                    row.get("bb_mid"),
                    row.get("bb_lower"),
                    row.get("atr_14"),
                    row.get("adx_14"),
                    row.get("volume_sma"),
                    row.get("macd_line"),
                    row.get("macd_signal"),
                    row.get("macd_hist"),
                    row.get("roc_20"),
                ),
            )


def get_todays_indicators(
    tickers: list[str],
    interval: str = "1d",
    for_date: Optional[datetime] = None,
) -> dict[str, dict]:
    """Bulk fetch indicator rows for the given tickers for a specific date.

    Parameters
    ----------
    for_date:
        UTC-aware datetime whose calendar date to query. Defaults to today (UTC).

    Returns {ticker: row_dict}. Tickers with no row for that date are absent.
    """
    if not tickers:
        return {}

    base = (for_date or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    today_start = base
    today_end = today_start + timedelta(days=1)

    engine = get_engine()
    query = text(
        """
        SELECT * FROM indicators
        WHERE ticker = ANY(:tickers)
          AND interval = :interval
          AND time >= :today_start
          AND time <  :today_end
        """
    )
    df = pd.read_sql(
        query,
        engine,
        params={"tickers": tickers, "interval": interval,
                "today_start": today_start, "today_end": today_end},
    )
    if df.empty:
        return {}
    return {row["ticker"]: row.to_dict() for _, row in df.iterrows()}


def get_latest_price_bars(
    tickers: list[str], interval: str = "1d"
) -> dict[str, dict]:
    """Return the most recent price bar per ticker as a dict keyed by ticker.

    Single query using DISTINCT ON. Returns {ticker: {close, volume, time}}.
    Used by indicators.py to attach close/volume to DB-cached indicator rows.
    """
    if not tickers:
        return {}

    engine = get_engine()
    query = text(
        """
        SELECT DISTINCT ON (ticker) ticker, close, volume, time
        FROM price_data
        WHERE ticker = ANY(:tickers) AND interval = :interval
        ORDER BY ticker, time DESC
        """
    )
    df = pd.read_sql(
        query, engine, params={"tickers": tickers, "interval": interval}
    )
    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        result[row["ticker"]] = {
            "close":  float(row["close"]),
            "volume": float(row["volume"]),
            "time":   row["time"],
        }
    return result


def get_latest_indicators(
    tickers: list[str], interval: str = "1d"
) -> dict[str, dict]:
    """Return the most recent indicator row per ticker in a single query.

    Uses DISTINCT ON so it always returns a result regardless of the bar date.
    This is what the simulator uses — indicators are timestamped at the last
    price bar (e.g. 2026-03-25 04:00 UTC) not at today's UTC calendar date,
    so date-filtered get_todays_indicators() would return nothing for the same
    calendar day the scan ran.

    Returns {ticker: row_dict}. Absent tickers have no data in the DB.
    """
    if not tickers:
        return {}

    engine = get_engine()
    query = text(
        """
        SELECT DISTINCT ON (ticker) *
        FROM indicators
        WHERE ticker = ANY(:tickers) AND interval = :interval
        ORDER BY ticker, time DESC
        """
    )
    df = pd.read_sql(
        query, engine, params={"tickers": tickers, "interval": interval}
    )
    if df.empty:
        return {}
    return {row["ticker"]: row.to_dict() for _, row in df.iterrows()}


def get_prev_indicators(
    tickers: list[str], interval: str = "1d"
) -> dict[str, dict]:
    """Return the second most recent indicator row per ticker.

    Uses ROW_NUMBER() DESC so weekends and holidays are handled correctly —
    never assumes the previous row is exactly one calendar day back.
    """
    if not tickers:
        return {}

    engine = get_engine()
    query = text(
        """
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY time DESC) AS rn
            FROM indicators
            WHERE ticker = ANY(:tickers) AND interval = :interval
        )
        SELECT * FROM ranked WHERE rn = 2
        """
    )
    df = pd.read_sql(
        query, engine, params={"tickers": tickers, "interval": interval}
    )
    if df.empty:
        return {}
    return {row["ticker"]: row.to_dict() for _, row in df.iterrows()}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def insert_signals(signals: list[dict], signal_time=None) -> list[int]:
    """Bulk insert strategy signals. Returns list of inserted ids in input order.

    Parameters
    ----------
    signal_time:
        Optional UTC-aware datetime/Timestamp to use for the signal time.
        When provided, anchors signals to the trading day they were derived from
        rather than the wall-clock time. Defaults to datetime.now(timezone.utc).

    Phase 3 uses the returned ids to call mark_signal_acted_on() after
    executing a simulated trade.
    """
    if not signals:
        return []

    now = signal_time if signal_time is not None else datetime.now(timezone.utc)
    if hasattr(now, 'to_pydatetime'):
        now = now.to_pydatetime()
    rows = [
        (
            now,
            s.get("ticker"),
            s.get("strategy"),
            s.get("signal_type", "").lower(),  # DB CHECK constraint requires lowercase
            s.get("strength"),
            s.get("reason"),
            json.dumps(s["indicator_vals"]) if s.get("indicator_vals") is not None else None,
            s.get("regime"),
            s.get("regime_confidence"),
            False,  # acted_on
            s.get("z_score"),
        )
        for s in signals
    ]

    with get_conn() as conn:
        with conn.cursor() as cur:
            result = psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO signals
                    (time, ticker, strategy, signal_type, strength, reason,
                     indicator_vals, regime, regime_confidence, acted_on, z_score)
                VALUES %s
                RETURNING id
                """,
                rows,
                fetch=True,
            )
            return [r[0] for r in result]


def get_todays_signals(
    min_strength: Optional[float] = None,
    for_date: Optional[datetime] = None,
) -> pd.DataFrame:
    """Return signals for a given date, ordered by strength descending.

    Parameters
    ----------
    for_date:
        UTC-aware datetime whose calendar date to query. Defaults to today (UTC).

    Always includes the 'id' column — Phase 3 uses it to call
    mark_signal_acted_on() after executing a simulated trade.
    """
    base = (for_date or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    today_start = base
    today_end = today_start + timedelta(days=1)

    engine = get_engine()
    conditions = ["time >= :today_start", "time < :today_end"]
    params: dict = {"today_start": today_start, "today_end": today_end}

    if min_strength is not None:
        conditions.append("strength >= :min_strength")
        params["min_strength"] = min_strength

    where = " AND ".join(conditions)
    query = text(f"SELECT * FROM signals WHERE {where} ORDER BY z_score DESC NULLS LAST, strength DESC")
    df = pd.read_sql(query, engine, params=params, parse_dates=["time"])
    return df


def get_open_position_tickers() -> list[str]:
    """Return tickers with a net positive position (buy qty > sell qty).

    Used by strategies as a duplicate-position guard — returns list[str] only.
    For full position details (Phase 3), use get_open_positions() instead.
    Do not merge these two functions.
    """
    engine = get_engine()
    query = text(
        """
        SELECT ticker
        FROM (
            SELECT ticker,
                   SUM(CASE WHEN side = 'buy'  THEN quantity ELSE 0 END) -
                   SUM(CASE WHEN side = 'sell' THEN quantity ELSE 0 END) AS net_qty
            FROM trades
            GROUP BY ticker
        ) t
        WHERE net_qty > 0
        ORDER BY ticker
        """
    )
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(query)]


# ---------------------------------------------------------------------------
# Phase 3: open positions (full DataFrame)
# ---------------------------------------------------------------------------

def get_open_positions() -> pd.DataFrame:
    """Return all currently open buy trades as a DataFrame.

    Open = status='filled' AND side='buy'. This matches how insert_trade() creates
    buy rows and how close_trade() sets status='closed'.

    Different from get_open_position_tickers() — returns full row data.
    Do NOT merge these two functions.
    """
    engine = get_engine()
    query = text(
        """
        SELECT id, time, ticker, quantity, fill_price, stop_loss, take_profit,
               signal_strength, strategy, reason, signal_data, status
        FROM trades
        WHERE side = 'buy' AND status = 'filled'
        ORDER BY time DESC
        """
    )
    df = pd.read_sql(query, engine, parse_dates=["time"])
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Phase 3: trade execution
# ---------------------------------------------------------------------------

def insert_trade(
    ticker: str,
    side: str,
    quantity: float,
    fill_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    strategy: str,
    signal_strength: Optional[float],
    reason: str,
    signal_data=None,
    trade_time: Optional[datetime] = None,
    fill_type: Optional[str] = None,
) -> int:
    """Insert a new trade row. Returns the new trade id.

    Monetary values are rounded to 2 decimal places before storage.
    FX conversion is tracked in the dedicated fx_rates table, not per-trade.

    trade_time:
        UTC-aware datetime to stamp the row with. Defaults to now().
        Pass the 9:31 AM ET open-cutoff so manual runs produce the same
        timestamp as the scheduled job.
    """
    now = trade_time if trade_time is not None else datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trades
                    (time, ticker, side, quantity, fill_price, stop_loss, take_profit,
                     signal_strength, strategy, reason, signal_data, status, fill_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'filled', %s)
                RETURNING id
                """,
                (
                    now,
                    ticker,
                    side.lower(),
                    quantity,
                    round(float(fill_price), 2),
                    round(float(stop_loss), 2) if stop_loss is not None else None,
                    round(float(take_profit), 2) if take_profit is not None else None,
                    float(signal_strength) if signal_strength is not None else None,
                    strategy,
                    reason,
                    json.dumps(signal_data) if signal_data is not None and not isinstance(signal_data, str) else signal_data,
                    fill_type,
                ),
            )
            row = cur.fetchone()
            return row[0]


def close_trade(ticker: str, exit_price: float, exit_reason: str, trade_time: Optional[datetime] = None) -> float:
    """Close an open buy trade for ticker in a single transaction.

    Actions (atomic):
      1. Find the most recent open buy trade for *ticker* (status='filled', side='buy').
      2. Insert a sell trade row with the same quantity, strategy, currency and fx_rate.
      3. Mark the original buy trade status='closed'.

    Returns
    -------
    float
        Realised P&L = (exit_price - original_fill_price) * quantity.
        Returns 0.0 if no open buy trade is found.
    """
    now = trade_time if trade_time is not None else datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Find most recent open buy trade
            cur.execute(
                """
                SELECT id, quantity, fill_price, strategy
                FROM trades
                WHERE ticker = %s AND side = 'buy' AND status = 'filled'
                ORDER BY time DESC
                LIMIT 1
                """,
                (ticker,),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning(f"close_trade: no open buy trade found for {ticker}")
                return 0.0

            orig_id, quantity, orig_fill, strategy = row
            realised_pnl = (float(exit_price) - float(orig_fill)) * float(quantity)

            # Insert sell trade
            cur.execute(
                """
                INSERT INTO trades
                    (time, ticker, side, quantity, fill_price, stop_loss, take_profit,
                     signal_strength, strategy, reason, signal_data, status)
                VALUES (%s, %s, 'sell', %s, %s, NULL, NULL, NULL, %s, %s, NULL, 'filled')
                """,
                (
                    now,
                    ticker,
                    float(quantity),
                    round(float(exit_price), 2),
                    strategy,
                    exit_reason,
                ),
            )

            # Mark original buy as closed
            cur.execute(
                "UPDATE trades SET status = 'closed' WHERE id = %s",
                (orig_id,),
            )

    return realised_pnl


def update_stop_loss(ticker: str, new_stop_loss: float) -> None:
    """Update stop_loss on the most recent open buy trade for ticker."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE trades
                SET stop_loss = %s
                WHERE id = (
                    SELECT id FROM trades
                    WHERE ticker = %s AND side = 'buy' AND status = 'filled'
                    ORDER BY time DESC
                    LIMIT 1
                )
                """,
                (round(float(new_stop_loss), 2), ticker),
            )


def mark_signal_acted_on(signal_id: int) -> None:
    """Set acted_on=True for the given signal id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signals SET acted_on = TRUE WHERE id = %s",
                (signal_id,),
            )


# ---------------------------------------------------------------------------
# Phase 3: analytics / reporting helpers
# ---------------------------------------------------------------------------

def get_trade_history(
    ticker: Optional[str] = None,
    strategy: Optional[str] = None,
    limit: int = 100,
) -> pd.DataFrame:
    """Return recent trades, optionally filtered by ticker and/or strategy.

    Parameters
    ----------
    ticker:
        If set, only return trades for this ticker.
    strategy:
        If set, only return trades for this strategy.
    limit:
        Maximum rows to return (default 100).
    """
    engine = get_engine()
    conditions = []
    params: dict = {"limit": limit}

    if ticker:
        conditions.append("ticker = :ticker")
        params["ticker"] = ticker
    if strategy:
        conditions.append("strategy = :strategy")
        params["strategy"] = strategy

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = text(
        f"SELECT * FROM trades {where} ORDER BY time DESC LIMIT :limit"
    )
    df = pd.read_sql(query, engine, params=params, parse_dates=["time"])
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def get_portfolio_stats() -> dict:
    """Return aggregate trade performance stats.

    Returns
    -------
    dict with keys:
        total_trades, winning_trades, win_rate, avg_win, avg_loss, total_pnl
    """
    engine = get_engine()
    # Join buy and sell trades to compute per-trade PnL
    query = text(
        """
        SELECT
            buy.ticker,
            buy.quantity,
            buy.fill_price                         AS buy_price,
            sell.fill_price                        AS sell_price,
            (sell.fill_price - buy.fill_price) * buy.quantity AS pnl
        FROM trades buy
        JOIN trades sell
          ON sell.ticker = buy.ticker
         AND sell.side   = 'sell'
         AND sell.strategy = buy.strategy
        WHERE buy.side = 'buy'
          AND buy.status = 'closed'
        """
    )
    df = pd.read_sql(query, engine)

    if df.empty:
        return {
            "total_trades":   0,
            "winning_trades": 0,
            "win_rate":       0.0,
            "avg_win":        0.0,
            "avg_loss":       0.0,
            "total_pnl":      0.0,
        }

    total  = len(df)
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    return {
        "total_trades":   total,
        "winning_trades": len(wins),
        "win_rate":       round(len(wins) / total, 4) if total > 0 else 0.0,
        "avg_win":        round(wins["pnl"].mean(), 2) if not wins.empty else 0.0,
        "avg_loss":       round(losses["pnl"].mean(), 2) if not losses.empty else 0.0,
        "total_pnl":      round(df["pnl"].sum(), 2),
    }


def get_latest_atr(ticker: str) -> Optional[float]:
    """Return the most recent atr_14 value for a ticker from the indicators table.

    Returns None if no row exists or atr_14 is NULL.
    """
    engine = get_engine()
    query = text(
        """
        SELECT atr_14 FROM indicators
        WHERE ticker = :ticker AND interval = '1d'
        ORDER BY time DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        val = conn.execute(query, {"ticker": ticker}).scalar()
    return float(val) if val is not None else None


def get_sector_map() -> dict[str, str]:
    """Return {ticker: sector} for all active watchlist tickers in one query.

    Used by simulator.py to pass sector context to executor without executor
    querying the DB directly. Unknown/NULL sectors become 'Unknown'.
    """
    engine = get_engine()
    query = text("SELECT ticker, sector FROM watchlist WHERE active = TRUE")
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
    return {row[0]: (row[1] if row[1] else "Unknown") for row in rows}


def get_market_map() -> dict[str, str]:
    """Return {ticker: market} for all active watchlist tickers.

    market is 'US' or 'CA'. Used to determine native currency for each ticker.
    """
    engine = get_engine()
    query = text("SELECT ticker, market FROM watchlist WHERE active = TRUE")
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
    return {row[0]: (row[1] if row[1] else "US") for row in rows}


def get_peak_portfolio_value() -> float:
    """Return the historical maximum total_value from portfolio_snapshots.

    Falls back to INITIAL_CAPITAL (.env) if no snapshots exist yet.
    Used by Portfolio.load_from_db() to restore peak_value on restart.
    """
    initial = float(os.environ.get("INITIAL_CAPITAL", 100_000.0))
    engine = get_engine()
    query = text("SELECT MAX(total_value) FROM portfolio_snapshots")
    with engine.connect() as conn:
        val = conn.execute(query).scalar()
    return float(val) if val is not None else initial


def get_latest_close_prices(tickers: list[str]) -> dict[str, float]:
    """Return the most recent closing price per ticker from price_data.

    Reads from the DB only — no yfinance call. The 5:00 PM data fetch writes
    today's close to price_data, so this is safe to call at 5:25 PM ET.
    Used by run_evening() so it never makes a live network request.

    Returns {ticker: close_price} for all requested tickers that have data.
    Tickers with no rows in price_data are simply absent from the result.
    """
    if not tickers:
        return {}

    engine = get_engine()
    query = text(
        """
        SELECT DISTINCT ON (ticker) ticker, close
        FROM price_data
        WHERE ticker = ANY(:tickers)
        ORDER BY ticker, time DESC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"tickers": tickers}).fetchall()
    return {row[0]: float(row[1]) for row in rows}
