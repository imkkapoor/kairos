"""
api/api.py — Kairos public read-only REST API (FastAPI).

Run with:
    cd backend && uvicorn api.api:app --host 0.0.0.0 --port 8000 --reload
"""

import copy
import json
import math
import os
import threading
from datetime import datetime
import time

import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from db.connection import (
    get_latest_snapshot,
    get_portfolio_history,
    get_trade_history,
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
