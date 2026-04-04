"""
api.py — Kairos public read-only REST API (FastAPI).

Run with:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""

import math
import os
from datetime import datetime

import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import connection as db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_ENV_PATH)

PORTFOLIO_CURRENCY: str = os.getenv("PORTFOLIO_CURRENCY", "CAD")

_METRICS_TICKERS = ["^GSPC", "CL=F", "CAD=X", "^VIX"]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Kairos API", docs_url="/api/docs", redoc_url=None)

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/portfolio")
def get_portfolio():
    """Latest active portfolio snapshot and full total-value history."""
    try:
        snapshot = db.get_latest_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No portfolio snapshot found")

        history_df = db.get_portfolio_history(days=365)
        history = []
        if not history_df.empty:
            for ts, row in history_df.iterrows():
                history.append({
                    "time": ts.isoformat(),
                    "total_value": float(row["total_value"]),
                })

        positions = snapshot.get("positions", {})
        # psycopg2 returns JSONB as a dict; guard against raw-string edge case
        if isinstance(positions, str):
            import json
            positions = json.loads(positions)

        return {
            "positions": _serialise(positions),
            "cash": float(snapshot["cash"]),
            "total_value": float(snapshot["total_value"]),
            "currency": snapshot.get("currency") or PORTFOLIO_CURRENCY,
            "history": history,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/trades")
def get_trades():
    """All recorded trades, newest first."""
    try:
        df = db.get_trade_history(limit=10_000)
        if df.empty:
            return []

        records = []
        for _, row in df.iterrows():
            records.append(_serialise(row.to_dict()))
        return records
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/fxrate")
def get_fxrate():
    """Current USD→PORTFOLIO_CURRENCY exchange rate, fetched live from yfinance."""
    try:
        if PORTFOLIO_CURRENCY == "USD":
            return {"pair": "USDUSD", "rate": 1.0, "currency": "USD"}

        pair_ticker = f"USD{PORTFOLIO_CURRENCY}=X"
        df = yf.download(pair_ticker, period="1d", interval="1m", progress=False, auto_adjust=True)
        if df is None or df.empty:
            raise HTTPException(status_code=503, detail=f"No FX data available for {pair_ticker}")

        close = df["Close"]
        if hasattr(close, "columns"):   # multi-ticker download guard
            close = close.iloc[:, 0]
        series = close.dropna()
        if series.empty:
            raise HTTPException(status_code=503, detail="FX close series is empty")

        rate = float(series.iloc[-1])
        return {
            "pair": f"USD{PORTFOLIO_CURRENCY}",
            "rate": round(rate, 6),
            "currency": PORTFOLIO_CURRENCY,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/metrics")
def get_metrics():
    """Intraday minute-bar data for ^GSPC, CL=F, CAD=X, and ^VIX."""
    try:
        result = {}

        for ticker in _METRICS_TICKERS:
            df = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=True)
            if df is None or df.empty:
                result[ticker] = None
                continue

            close = df["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]

            open_col = df["Open"]
            if hasattr(open_col, "columns"):
                open_col = open_col.iloc[:, 0]

            series = close.dropna()
            if series.empty:
                result[ticker] = None
                continue

            current = float(series.iloc[-1])
            open_price = float(open_col.dropna().iloc[0]) if not open_col.dropna().empty else None

            change = round(current - open_price, 4) if open_price is not None else None
            change_pct = (
                round((current - open_price) / open_price * 100, 4)
                if open_price is not None and open_price != 0
                else None
            )

            bars = [
                {
                    "time": ts.isoformat(),
                    "close": round(float(val), 4),
                }
                for ts, val in series.items()
            ]

            result[ticker] = {
                "current": round(current, 4),
                "change": change,
                "change_pct": change_pct,
                "bars": bars,
            }

        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
