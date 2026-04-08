"""
strategies/vol_regime.py — VIX-based volatility regime classification.

Pure classification logic — no DB calls, no yfinance. Designed to be used
by both the backtester (via get_vix_regime) and the live executor (future import).

Regime thresholds:
    VIX <  20  → NORMAL   (size_mult=1.00)
    VIX <  30  → ELEVATED (size_mult=0.65)
    VIX <  40  → HIGH     (size_mult=0.35)
    VIX >= 40  → EXTREME  (size_mult=0.00)

Strategy suppression by regime:
    ELEVATED : reversal
    HIGH     : reversal, sector_rotation, macd
    EXTREME  : rsi, momentum, macd, reversal, sector_rotation  (all)
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Regime thresholds
# ---------------------------------------------------------------------------

_THRESHOLDS = [
    (20.0, "NORMAL",   1.00),
    (30.0, "ELEVATED", 0.65),
    (40.0, "HIGH",     0.35),
]
_EXTREME = ("EXTREME", 0.00)

# ---------------------------------------------------------------------------
# Strategy suppression sets (empty set = no suppression)
# ---------------------------------------------------------------------------

SUPPRESSED_STRATEGIES: dict[str, set[str]] = {
    "NORMAL":   set(),
    "ELEVATED": {"reversal"},
    "HIGH":     {"reversal", "sector_rotation", "macd"},
    "EXTREME":  {"rsi", "momentum", "macd", "reversal", "sector_rotation"},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_vix(vix_value: Optional[float]) -> dict:
    """Classify a VIX value into a regime. Pure function — no DB calls.

    Parameters
    ----------
    vix_value:
        Current VIX close price, or None. None → NORMAL (fail-open).

    Returns
    -------
    dict with keys:
        regime     : str   — one of NORMAL / ELEVATED / HIGH / EXTREME
        size_mult  : float — position sizing multiplier (0.0–1.0)
    """
    if vix_value is None:
        return {"regime": "NORMAL", "size_mult": 1.0}

    v = float(vix_value)
    for threshold, regime, mult in _THRESHOLDS:
        if v < threshold:
            return {"regime": regime, "size_mult": mult}
    regime, mult = _EXTREME
    return {"regime": regime, "size_mult": mult}


def get_vix_regime(as_of_date: date, vix_series: pd.Series) -> dict:
    """Look up VIX regime for a specific date from a pre-loaded Series.

    Designed to be called once per trading day in the backtester using a
    Series loaded once per WFA window — not once per day.

    Fallback chain:
        1. Exact date match in vix_series.
        2. Last known value before as_of_date (handles weekends/holidays).
        3. NORMAL if no data at all (fail-open).

    Parameters
    ----------
    as_of_date:
        The trading day to look up.
    vix_series:
        pd.Series indexed by UTC Timestamps (or dates), values = VIX close.
        Returned by data_loader.load_vix() — already forward-filled.

    Returns
    -------
    dict with keys:
        vix        : float | None  — the VIX value used (None if no data)
        regime     : str
        size_mult  : float
        suppressed : set[str]      — strategy names to suppress today
    """
    if vix_series is None or vix_series.empty:
        return {
            "vix": None,
            "regime": "NORMAL",
            "size_mult": 1.0,
            "suppressed": set(),
        }

    # Normalise the index to UTC-midnight Timestamps for comparison
    target_ts = pd.Timestamp(as_of_date, tz="UTC")

    vix_value: Optional[float] = None

    # Exact day match
    if target_ts in vix_series.index:
        vix_value = float(vix_series.loc[target_ts])
    else:
        # Most recent value on or before as_of_date
        candidates = vix_series[vix_series.index <= target_ts]
        if not candidates.empty:
            vix_value = float(candidates.iloc[-1])

    classification = classify_vix(vix_value)
    regime = classification["regime"]

    return {
        "vix": vix_value,
        "regime": regime,
        "size_mult": classification["size_mult"],
        "suppressed": SUPPRESSED_STRATEGIES.get(regime, set()).copy(),
    }
