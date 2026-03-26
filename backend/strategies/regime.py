"""
strategies/regime.py — Market regime detection.

THREE REGIMES:
  TRENDING — ADX > 25
  CHOPPY   — ADX < 20
  CRISIS   — SPY close down > 5% vs 5 trading days ago
             (SPY used as proxy for all tickers including Canadian — deliberate)

CONFIDENCE (linear interpolation):
  TRENDING: ADX >= 35 → 1.0 | ADX = 25 → 0.0 | linear between
  CHOPPY:   ADX <= 12 → 1.0 | ADX = 20 → 0.0 | linear between
  ADX 20-25 ambiguous zone: assign lower of the two confidences
  CRISIS: always 1.0
  Missing indicator row: CHOPPY, confidence 0.5

Stateless — no DB access. All inputs passed as arguments.
"""

import pandas as pd


# ---------------------------------------------------------------------------
# Confidence helpers
# ---------------------------------------------------------------------------

def _trending_confidence(adx: float) -> float:
    """Linear interpolation: ADX=25 → 0.0, ADX≥35 → 1.0."""
    if adx >= 35.0:
        return 1.0
    if adx <= 25.0:
        return 0.0
    return (adx - 25.0) / 10.0


def _choppy_confidence(adx: float) -> float:
    """Linear interpolation: ADX≤12 → 1.0, ADX=20 → 0.0."""
    if adx <= 12.0:
        return 1.0
    if adx >= 20.0:
        return 0.0
    return (20.0 - adx) / 8.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_crisis(spy_df: pd.DataFrame) -> bool:
    """Return True if SPY is in crisis (down > 5% vs 5 trading days ago).

    Requires at least 6 rows in spy_df; returns False if insufficient data.
    """
    if spy_df is None or spy_df.empty or len(spy_df) < 6:
        return False
    close      = spy_df["close"].astype(float)
    current    = close.iloc[-1]
    five_ago   = close.iloc[-6]
    if five_ago == 0:
        return False
    return (current - five_ago) / five_ago < -0.05


def detect_all(
    indicator_rows: dict[str, dict],
    spy_df: pd.DataFrame,
) -> dict[str, dict]:
    """Detect market regime for each ticker present in *indicator_rows*.

    Returns {ticker: {"regime": str, "confidence": float}}.

    Tickers not in *indicator_rows* are absent from the result; callers should
    default to {"regime": "CHOPPY", "confidence": 0.5} for missing entries.
    """
    is_crisis = detect_crisis(spy_df)
    results: dict[str, dict] = {}

    for ticker, row in indicator_rows.items():
        if is_crisis:
            results[ticker] = {"regime": "CRISIS", "confidence": 1.0}
            continue

        adx = row.get("adx_14")
        if adx is None:
            results[ticker] = {"regime": "CHOPPY", "confidence": 0.5}
            continue

        try:
            adx = float(adx)
            import math
            if math.isnan(adx):
                results[ticker] = {"regime": "CHOPPY", "confidence": 0.5}
                continue
        except (TypeError, ValueError):
            results[ticker] = {"regime": "CHOPPY", "confidence": 0.5}
            continue

        if adx > 25.0:
            results[ticker] = {"regime": "TRENDING", "confidence": _trending_confidence(adx)}
        elif adx < 20.0:
            results[ticker] = {"regime": "CHOPPY",   "confidence": _choppy_confidence(adx)}
        else:
            # Ambiguous zone (ADX 20-25): assign the regime with the lower confidence
            t_conf = _trending_confidence(adx)
            c_conf = _choppy_confidence(adx)
            if t_conf <= c_conf:
                results[ticker] = {"regime": "TRENDING", "confidence": t_conf}
            else:
                results[ticker] = {"regime": "CHOPPY",   "confidence": c_conf}

    return results
