"""
api/api.py — Kairos public read-only REST API (FastAPI).

Run with:
    cd backend && uvicorn api.api:app --host 0.0.0.0 --port 8000 --reload
"""

import copy
import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
import time

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from db.connection import (
    get_latest_snapshot,
    get_portfolio_history,
    get_trade_history,
    get_backtest_results,
    get_backtest_summary,
    get_backtest_run_list,
    get_price_data,
    get_backtest_analytics,
    get_vix_range,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)

PORTFOLIO_CURRENCY: str = os.getenv("PORTFOLIO_CURRENCY", "CAD")

_METRICS_TICKERS = ["^GSPC", "CL=F", "CAD=X", "^VIX"]
# Protects concurrent yfinance download calls across simultaneous API requests
_yf_lock = threading.Lock()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Kairos API", docs_url="/api/docs", redoc_url=None)


@app.middleware("http")
async def _log_request_time(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    logger.info(f"{request.method} {request.url.path} → {response.status_code} ({ms:.0f}ms)")
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialise(value):
    """Recursively convert non-JSON-serialisable values."""
    if isinstance(value, dict):
        return {k: _serialise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialise(v) for v in value]
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # pandas Timestamp, numpy scalars, etc.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):          # numpy scalar → Python native
        v = value.item()
        if isinstance(v, float) and math.isnan(v):
            return None
        return v
    return value


def _get_series(df, col: str, ticker: str):
    """Extract a single-ticker Series from a potentially multi-indexed DataFrame."""
    try:
        frame = df[col]
        if hasattr(frame, "columns"):
            return frame[ticker].dropna() if ticker in frame.columns else None
        return frame.dropna()
    except (KeyError, Exception):
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def get_dashboard():
    """Portfolio snapshot + live market metrics via a single yfinance batch call."""
    try:
        # --- DB reads -------------------------------------------------------
        snapshot = get_latest_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No portfolio snapshot found")

        history_df = get_portfolio_history(days=365)
        history = []
        if not history_df.empty:
            for ts, row in history_df.iterrows():
                history.append({"time": ts.isoformat(), "total_value": float(row["total_value"])})

        positions = copy.deepcopy(snapshot.get("positions", {}))
        if isinstance(positions, str):
            positions = json.loads(positions)

        # --- Single yfinance batch call (positions + market metrics) --------
        position_tickers = list(positions.keys())
        # Preserve order, deduplicate: positions first so _col works correctly
        all_tickers = list(dict.fromkeys(position_tickers + _METRICS_TICKERS))

        df = None
        if all_tickers:
            with _yf_lock:
                df = yf.download(
                    all_tickers, period="1d", interval="1m",
                    progress=False, auto_adjust=True,
                )

        # --- Parse live prices for positions --------------------------------
        for ticker in position_tickers:
            close_s = _get_series(df, "Close", ticker) if df is not None else None
            open_s  = _get_series(df, "Open",  ticker) if df is not None else None

            if close_s is None or close_s.empty:
                positions[ticker].update({"price": None, "change_today": None, "change_today_pct": None})
                continue

            price      = round(float(close_s.iloc[-1]), 4)
            open_price = float(open_s.iloc[0]) if open_s is not None and not open_s.empty else None
            change     = round(price - open_price, 4) if open_price is not None else None
            change_pct = (
                round((price - open_price) / open_price * 100, 4)
                if open_price is not None and open_price != 0 else None
            )
            positions[ticker].update({"price": price, "change_today": change, "change_today_pct": change_pct})

        # --- Parse market metrics -------------------------------------------
        metrics: dict = {}
        if df is None or df.empty:
            metrics = {t: None for t in _METRICS_TICKERS}
        else:
            for ticker in _METRICS_TICKERS:
                close_s = _get_series(df, "Close", ticker)
                open_s  = _get_series(df, "Open",  ticker)

                if close_s is None or close_s.empty:
                    metrics[ticker] = None
                    continue

                current    = float(close_s.iloc[-1])
                open_price = float(open_s.iloc[0]) if open_s is not None and not open_s.empty else None
                change     = round(current - open_price, 4) if open_price is not None else None
                change_pct = (
                    round((current - open_price) / open_price * 100, 4)
                    if open_price is not None and open_price != 0 else None
                )
                bars = [
                    {"time": ts.isoformat(), "close": round(float(val), 4)}
                    for ts, val in close_s.items()
                ]
                metrics[ticker] = {
                    "current": round(current, 4),
                    "change": change,
                    "change_pct": change_pct,
                    "bars": bars,
                }

        return {
            "portfolio": {
                "positions": _serialise(positions),
                "cash": float(snapshot["cash"]),
                "total_value": float(snapshot["total_value"]),
                "currency": snapshot.get("currency") or PORTFOLIO_CURRENCY,
                "history": history,
            },
            "metrics": metrics,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/trades")
def get_trades():
    """All recorded trades, newest first."""
    try:
        df = get_trade_history(limit=10_000)
        if df.empty:
            return []

        records = []
        for _, row in df.iterrows():
            records.append(_serialise(row.to_dict()))
        return records
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/backtest")
def get_backtest(config: str | None = None, run_id: str | None = None):
    """ROOS backtest results. Optional ?config=name and/or ?run_id=UUID to filter."""
    try:
        df = get_backtest_results(config_name=config, run_id=run_id)
        if df.empty:
            return {"runs": [], "summary": [], "run_list": []}

        runs = [_serialise(row.to_dict()) for _, row in df.iterrows()]

        summary_df = get_backtest_summary(run_id=run_id)
        summary = [_serialise(row.to_dict()) for _, row in summary_df.iterrows()]

        run_list_df = get_backtest_run_list()
        run_list = [_serialise(row.to_dict()) for _, row in run_list_df.iterrows()]

        return {"runs": runs, "summary": summary, "run_list": run_list}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/backtest/runs")
def get_backtest_runs():
    """List all distinct backtest executions (one row per run_id)."""
    try:
        df = get_backtest_run_list()
        return {"runs": [_serialise(row.to_dict()) for _, row in df.iterrows()]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/backtest/analytics")
def get_analytics(run_id: str):
    """Pre-computed chart data (equity curve, drawdown, heatmaps) for a backtest run."""
    try:
        df = get_backtest_results(run_id=run_id)
        if df.empty:
            return {"analytics": []}
        backtest_ids = df["id"].tolist()
        rows = get_backtest_analytics(backtest_ids)
        return {"analytics": [_serialise(r) for r in rows]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/backtest/vix")
def get_backtest_vix(run_id: str):
    """VIX daily close aligned to the backtest window — used to overlay on equity curve."""
    try:
        df = get_backtest_results(run_id=run_id)
        if df.empty:
            return {"timestamps": [], "vix": []}
        window_start = df["window_start"].min()
        window_end = df["window_end"].max()
        vix = get_vix_range(window_start, window_end)
        if vix.empty:
            return {"timestamps": [], "vix": []}
        timestamps = [ts.strftime("%Y-%m-%d") for ts in vix.index]
        values = [round(float(v), 2) if not math.isnan(v) else None for v in vix.values]
        return {"timestamps": timestamps, "vix": values}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Ticker chart
# ---------------------------------------------------------------------------

# Allowed characters in a ticker symbol — prevents path-traversal/injection
_TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,20}$")

# Map range label → how to fetch data
_RANGE_CONFIG: dict[str, dict] = {
    "1D":  {"source": "live", "period": "1d",   "yf_interval": "1m"},
    "1W":  {"source": "db",   "days": 7,        "interval": "1d"},
    "1M":  {"source": "db",   "days": 30,       "interval": "1d"},
    "3M":  {"source": "db",   "days": 90,       "interval": "1d"},
    "6M":  {"source": "db",   "days": 180,      "interval": "1d"},
    "YTD": {"source": "db",   "ytd": True,      "interval": "1d"},
    "1Y":  {"source": "db",   "days": 365,      "interval": "1d"},
    "5Y":  {"source": "db",   "days": 365 * 5,  "interval": "1d"},
}


@app.get("/api/ticker/{ticker}")
def get_ticker_chart(ticker: str, range: str = "1M"):
    """OHLCV bars + trades for a single ticker. Used by the position drawer."""
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")
    if range not in _RANGE_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid range. Must be one of: {', '.join(_RANGE_CONFIG)}",
        )

    cfg = _RANGE_CONFIG[range]

    try:
        bars: list[dict] = []

        if cfg["source"] == "live":
            with _yf_lock:
                raw = yf.download(
                    ticker,
                    period=cfg["period"],
                    interval=cfg["yf_interval"],
                    progress=False,
                    auto_adjust=True,
                )
            if not raw.empty:
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.get_level_values(0)
                raw.columns = [str(c).lower().strip() for c in raw.columns]
                if raw.index.tz is None:
                    raw.index = raw.index.tz_localize("UTC")
                else:
                    raw.index = raw.index.tz_convert("UTC")
                for ts, row in raw.iterrows():
                    bars.append({
                        "time":   ts.isoformat(),
                        "open":   round(float(row["open"]),  4) if pd.notna(row["open"])  else None,
                        "high":   round(float(row["high"]),  4) if pd.notna(row["high"])  else None,
                        "low":    round(float(row["low"]),   4) if pd.notna(row["low"])   else None,
                        "close":  round(float(row["close"]), 4) if pd.notna(row["close"]) else None,
                        "volume": int(row["volume"])             if pd.notna(row["volume"]) else None,
                    })

        else:
            now = datetime.now(timezone.utc)
            if cfg.get("ytd"):
                start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
            else:
                start = now - timedelta(days=cfg["days"])

            price_df = get_price_data(ticker, interval=cfg["interval"], start=start)
            if not price_df.empty:
                for ts, row in price_df.iterrows():
                    bars.append({
                        "time":   ts.isoformat(),
                        "open":   round(float(row["open"]),  4) if pd.notna(row["open"])  else None,
                        "high":   round(float(row["high"]),  4) if pd.notna(row["high"])  else None,
                        "low":    round(float(row["low"]),   4) if pd.notna(row["low"])   else None,
                        "close":  round(float(row["close"]), 4) if pd.notna(row["close"]) else None,
                        "volume": int(row["volume"])             if pd.notna(row["volume"]) else None,
                    })

        trades_df = get_trade_history(ticker=ticker, limit=10_000)
        trades = (
            [_serialise(row.to_dict()) for _, row in trades_df.iterrows()]
            if not trades_df.empty
            else []
        )

        return {"ticker": ticker, "range": range, "bars": bars, "trades": trades}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
