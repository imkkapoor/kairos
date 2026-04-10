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


def is_vix_spike(
    vix_series: pd.Series,
    as_of_date: date,
    sma_window: int = 10,
    threshold: float = 0.20,
) -> bool:
    """Return True if today's VIX is more than `threshold` above its trailing SMA.

    Pure function — no DB calls, no side effects. Uses only data up to and
    including as_of_date (no lookahead).

    Parameters
    ----------
    vix_series:
        pd.Series indexed by UTC Timestamps, values = VIX close.
        Already forward-filled by data_loader.load_vix().
    as_of_date:
        The trading day to evaluate.
    sma_window:
        Number of prior trading days to use for the SMA (default 10).
    threshold:
        Fractional threshold above SMA to trigger spike (default 0.20 = 20%).

    Returns
    -------
    bool — True if today_vix > sma * (1 + threshold), False otherwise.
    Returns False if fewer than sma_window data points precede as_of_date.
    """
    if vix_series is None or vix_series.empty:
        return False

    target_ts = pd.Timestamp(as_of_date, tz="UTC")

    # All data up to and including as_of_date
    prior = vix_series[vix_series.index <= target_ts]
    if len(prior) < sma_window:
        return False

    today_vix = float(prior.iloc[-1])
    # SMA uses the last sma_window values including today
    sma = float(prior.iloc[-sma_window:].mean())
    if sma <= 0:
        return False

    return today_vix > sma * (1.0 + threshold)


def get_vix_regime(
    as_of_date: date,
    vix_series: pd.Series,
    sma_window: int = 10,
    vroc_threshold: float = 0.20,
) -> dict:
    """Look up VIX regime for a specific date from a pre-loaded Series.

    Designed to be called once per trading day in the backtester using a
    Series loaded once per ROOS window — not once per day.

    Applies a VROC spike override after the base classify_vix() result:
    if is_vix_spike() fires and the absolute regime is NORMAL or ELEVATED,
    the regime is forced to HIGH (size_mult=0.35). The spike only upgrades,
    never downgrades.

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
    sma_window:
        Trailing SMA window for the VROC spike check (default 10 days).
    vroc_threshold:
        Fractional threshold above SMA to trigger spike (default 0.20 = 20%).

    Returns
    -------
    dict with keys:
        vix             : float | None  — the VIX value used (None if no data)
        regime          : str
        size_mult       : float
        suppressed      : set[str]      — strategy names to suppress today
        spike_triggered : bool          — True if VROC spike fired today
    """
    if vix_series is None or vix_series.empty:
        return {
            "vix": None,
            "regime": "NORMAL",
            "size_mult": 1.0,
            "suppressed": set(),
            "spike_triggered": False,
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
    regime    = classification["regime"]
    size_mult = classification["size_mult"]

    # VROC spike override — only upgrades, never downgrades
    spike = is_vix_spike(vix_series, as_of_date, sma_window, vroc_threshold)
    if spike and regime in ("NORMAL", "ELEVATED"):
        regime    = "HIGH"
        size_mult = 0.35

    return {
        "vix": vix_value,
        "regime": regime,
        "size_mult": size_mult,
        "suppressed": SUPPRESSED_STRATEGIES.get(regime, set()).copy(),
        "spike_triggered": spike,
    }
