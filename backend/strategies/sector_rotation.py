"""
strategies/sector_rotation.py — Sector rotation signal generator.

Detects macro regime shifts using ETF relative performance and generates
signals for tickers in favored/unfavored sectors.

Late-cycle (XLE outperforming XLK + rising rates):
  Favored: Energy, Materials, Industrials
  Unfavored: Technology, Consumer Discretionary

Defensive (XLU/XLV outperforming SPY):
  Favored: Utilities, Healthcare, Consumer Staples
  Unfavored: Technology, Financials

BUY:  ticker in favored sector AND ma_50 > ma_200 AND adx_14 > 15
SELL: ticker in unfavored sector AND close < ma_50

Strength:
  base = min(abs(sector_score) / 10, 0.5) * (adx / 40), max 0.5
  regime_mult: TRENDING=1.0, CHOPPY=0.6, CRISIS=0.1
  final = min(base * regime_mult * regime_conf, 1.0)

Stateless — no DB access. All inputs passed as arguments.
"""

import math


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nan(v) -> bool:
    if v is None:
        return True
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return True


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


_REGIME_MULT = {
    "TRENDING": 1.0,
    "CHOPPY":   0.6,
    "CRISIS":   0.1,
}

_DEFAULT_REGIME = {"regime": "CHOPPY", "confidence": 0.5}

# Sector classification for signal logic
_LATE_CYCLE_FAVORED   = {"Energy", "Materials", "Industrials"}
_LATE_CYCLE_UNFAVORED = {"Technology", "Consumer Discretionary"}
_DEFENSIVE_FAVORED    = {"Utilities", "Healthcare", "Consumer Staples"}
_DEFENSIVE_UNFAVORED  = {"Technology", "Financials"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_signals(
    tickers: list[str],
    indicator_rows: dict[str, dict],
    regimes: dict[str, dict],
    open_position_tickers: list[str],
    sector_scores: dict[str, float] | None,
    sector_map: dict[str, str],
) -> list[dict]:
    """Generate sector rotation signals for *tickers*.

    Parameters
    ----------
    sector_scores:
        Pre-computed by scanner: {'late_cycle': float, 'defensive': float}.
        If None, sector rotation is disabled (missing ETF data).
    sector_map:
        {ticker: sector} from watchlist.

    Returns a list of signal dicts ready for ``insert_signals()``.
    """
    if sector_scores is None:
        return []

    open_set = set(open_position_tickers)
    signals: list[dict] = []

    late_cycle = sector_scores.get("late_cycle", 0.0)
    defensive  = sector_scores.get("defensive", 0.0)

    # Determine which sectors are currently favored/unfavored
    favored: set[str] = set()
    unfavored: set[str] = set()

    if late_cycle > 5:
        favored |= _LATE_CYCLE_FAVORED
        unfavored |= _LATE_CYCLE_UNFAVORED
    if defensive > 3:
        favored |= _DEFENSIVE_FAVORED
        unfavored |= _DEFENSIVE_UNFAVORED

    # No clear macro signal — nothing to do
    if not favored and not unfavored:
        return []

    for ticker in tickers:
        row = indicator_rows.get(ticker)
        if row is None:
            continue

        sector = sector_map.get(ticker, "Unknown")
        if sector == "Unknown":
            continue

        ma50       = row.get("ma_50")
        ma200      = row.get("ma_200")
        close      = row.get("close")
        adx        = row.get("adx_14")

        if any(_nan(x) for x in [ma50, ma200, close]):
            continue

        ma50_f  = float(ma50)
        ma200_f = float(ma200)
        close_f = float(close)
        adx_f   = float(adx) if not _nan(adx) else 0.0

        reg   = regimes.get(ticker, _DEFAULT_REGIME)
        rmult = _REGIME_MULT.get(reg["regime"], 1.0)
        rconf = float(reg["confidence"])

        interval = row.get("interval", "1d")

        # Determine which score is driving this signal
        if sector in favored and sector in _LATE_CYCLE_FAVORED:
            active_score = late_cycle
        elif sector in favored and sector in _DEFENSIVE_FAVORED:
            active_score = defensive
        elif sector in unfavored:
            # Use whichever score is driving the unfavored classification
            active_score = late_cycle if sector in _LATE_CYCLE_UNFAVORED else defensive
        else:
            continue

        # BUY: favored sector + basic uptrend + some trend exists
        if sector in favored and ma50_f > ma200_f and adx_f > 15:
            if ticker in open_set:
                continue
            base = min(abs(active_score) / 10, 0.5) * (adx_f / 40)
            base = _clamp(base, 0.0, 0.5)
            final = min(base * rmult * rconf, 1.0)
            reason = (
                f"Sector rotation: {sector} favored "
                f"(late_cycle={late_cycle:.1f}, defensive={defensive:.1f}), "
                f"MA trend confirmed, ADX={adx_f:.1f}, "
                f"regime={reg['regime']}"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "sector_rotation",
                "signal_type": "BUY",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

        # SELL: unfavored sector + price below MA50
        elif sector in unfavored and close_f < ma50_f:
            base = min(abs(active_score) / 10, 0.5) * (adx_f / 40)
            base = _clamp(base, 0.0, 0.5)
            final = min(base * rmult * rconf, 1.0)
            reason = (
                f"Sector rotation: {sector} unfavored "
                f"(late_cycle={late_cycle:.1f}, defensive={defensive:.1f}), "
                f"close={close_f:.2f}<ma50={ma50_f:.2f}, ADX={adx_f:.1f}, "
                f"regime={reg['regime']}"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "sector_rotation",
                "signal_type": "SELL",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

    return signals
