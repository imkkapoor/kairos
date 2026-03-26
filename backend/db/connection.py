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
    date,
    tickers_total: int,
    tickers_success: int,
    tickers_skipped: int,
    tickers_failed: int,
    failed_tickers: list,
    rows_inserted: int,
    duration_secs: float,
    notes: Optional[str] = None,
) -> None:
    """Upsert one summary row per calendar day into fetch_log.

    ON CONFLICT (date) updates all fields so re-runs overwrite the previous entry.
    """
    failed_str = ",".join(failed_tickers) if failed_tickers else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fetch_log
                    (date, tickers_total, tickers_success, tickers_skipped,
                     tickers_failed, failed_tickers, rows_inserted, duration_secs, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (date) DO UPDATE SET
                    tickers_total   = EXCLUDED.tickers_total,
                    tickers_success = EXCLUDED.tickers_success,
                    tickers_skipped = EXCLUDED.tickers_skipped,
                    tickers_failed  = EXCLUDED.tickers_failed,
                    failed_tickers  = EXCLUDED.failed_tickers,
                    rows_inserted   = EXCLUDED.rows_inserted,
                    duration_secs   = EXCLUDED.duration_secs,
                    notes           = EXCLUDED.notes
                """,
                (date, tickers_total, tickers_success, tickers_skipped,
                 tickers_failed, failed_str, rows_inserted, duration_secs, notes),
            )


def get_fetch_history(days: int = 30) -> pd.DataFrame:
    """Return the last *days* rows from fetch_log, ordered by date DESC."""
    engine = get_engine()
    query = text(
        "SELECT * FROM fetch_log ORDER BY date DESC LIMIT :days"
    )
    return pd.read_sql(query, engine, params={"days": days}, parse_dates=["date", "created_at"])


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
) -> None:
    """Persist a point-in-time portfolio snapshot.

    Parameters
    ----------
    cash:
        Current cash balance in dollars.
    total_value:
        Total portfolio value (cash + open positions) in dollars.
    positions:
        JSON-serialisable dict mapping ticker → position details.
        Must include enough context for Phase 3 position management.
    daily_pnl:
        Profit/loss for the current trading day (can be None).
    total_pnl:
        Cumulative profit/loss since inception (can be None).
    drawdown:
        Current drawdown as a fraction of peak value (can be None).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO portfolio_snapshots
                    (cash, total_value, positions, daily_pnl, total_pnl, drawdown)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    cash,
                    total_value,
                    json.dumps(positions),
                    daily_pnl,
                    total_pnl,
                    drawdown,
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
                     atr_14, adx_14, volume_sma)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )


def get_todays_indicators(
    tickers: list[str], interval: str = "1d"
) -> dict[str, dict]:
    """Bulk fetch today's indicator rows for the given tickers in a single query.

    Returns {ticker: row_dict}. Tickers with no row for today are absent from the result.
    """
    if not tickers:
        return {}

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
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

def insert_signals(signals: list[dict]) -> list[int]:
    """Bulk insert strategy signals. Returns list of inserted ids in input order.

    Phase 3 uses the returned ids to call mark_signal_acted_on() after
    executing a simulated trade.
    """
    if not signals:
        return []

    now = datetime.now(timezone.utc)
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
                     indicator_vals, regime, regime_confidence, acted_on)
                VALUES %s
                RETURNING id
                """,
                rows,
                fetch=True,
            )
            return [r[0] for r in result]


def get_todays_signals(min_strength: Optional[float] = None) -> pd.DataFrame:
    """Return today's signals ordered by strength descending.

    Always includes the 'id' column — Phase 3 uses it to call
    mark_signal_acted_on() after executing a simulated trade.
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_end = today_start + timedelta(days=1)

    engine = get_engine()
    conditions = ["time >= :today_start", "time < :today_end"]
    params: dict = {"today_start": today_start, "today_end": today_end}

    if min_strength is not None:
        conditions.append("strength >= :min_strength")
        params["min_strength"] = min_strength

    where = " AND ".join(conditions)
    query = text(f"SELECT * FROM signals WHERE {where} ORDER BY strength DESC")
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
