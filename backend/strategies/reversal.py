"""
strategies/reversal.py — Short-term reversal signal generator.

Academic basis: Jegadeesh (1990) — stocks with worst 1-month return
tend to outperform the next month. Works on liquid large-cap stocks.

BUY:  ticker in bottom 10% of 20-day ROC rankings AND rsi_14 > 20 AND roc_20 < -3
SELL: roc_20 > 5 (recovery exit)

Strength:
  base = min((0.10 - percentile) / 0.10, 1.0) * abs(roc_20) / 20, clamped 0.0-1.0
  regime_mult: CHOPPY=1.0, TRENDING=0.3, CRISIS=0.05
  volume_mult: clamp(volume/volume_sma, 0.5, 1.5)
  final = min(base * regime_mult * regime_conf * vol_mult, 1.0)

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


def _volume_mult(volume, volume_sma) -> float:
    if _nan(volume) or _nan(volume_sma) or float(volume_sma) == 0:
        return 1.0
    return _clamp(float(volume) / float(volume_sma), 0.5, 1.5)


_REGIME_MULT = {
    "CHOPPY":   1.0,
    "TRENDING": 0.3,
    "CRISIS":   0.05,
}

_DEFAULT_REGIME = {"regime": "CHOPPY", "confidence": 0.5}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_signals(
    tickers: list[str],
    indicator_rows: dict[str, dict],
    regimes: dict[str, dict],
    open_position_tickers: list[str],
    roc_rankings: dict[str, float],
) -> list[dict]:
    """Generate short-term reversal signals for *tickers*.

    Parameters
    ----------
    roc_rankings:
        Pre-computed by scanner: {ticker: percentile} where
        percentile = rank_index / total_tickers (0.0 = worst, 1.0 = best).

    Returns a list of signal dicts ready for ``insert_signals()``.
    """
    open_set = set(open_position_tickers)
    signals: list[dict] = []

    for ticker in tickers:
        row = indicator_rows.get(ticker)
        if row is None:
            continue

        roc_20     = row.get("roc_20")
        rsi        = row.get("rsi_14")
        volume     = row.get("volume")
        volume_sma = row.get("volume_sma")

        if _nan(roc_20) or _nan(rsi):
            continue

        roc_f = float(roc_20)
        rsi_f = float(rsi)

        reg   = regimes.get(ticker, _DEFAULT_REGIME)
        rmult = _REGIME_MULT.get(reg["regime"], 1.0)
        rconf = float(reg["confidence"])
        vmult = _volume_mult(volume, volume_sma)

        interval = row.get("interval", "1d")

        percentile = roc_rankings.get(ticker)
        if percentile is None:
            continue

        # BUY: bottom 10% of ROC ranking, RSI > 20 (not freefall), ROC < -3
        if percentile < 0.10 and rsi_f > 20 and roc_f < -3:
            if ticker in open_set:
                continue
            base = _clamp(
                min((0.10 - percentile) / 0.10, 1.0) * abs(roc_f) / 20,
                0.0, 1.0,
            )
            final = min(base * rmult * rconf * vmult, 1.0)
            reason = (
                f"Short-term reversal: ROC={roc_f:.1f}% "
                f"(bottom {percentile * 100:.0f}% of universe), "
                f"RSI={rsi_f:.1f}, "
                f"regime={reg['regime']}(conf={rconf:.2f})"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "reversal",
                "signal_type": "BUY",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

        # SELL: recovery exit — roc > 5
        elif ticker in open_set and roc_f > 5:
            base = _clamp(roc_f / 20, 0.0, 1.0)
            final = min(base * rmult * rconf * vmult, 1.0)
            reason = (
                f"Reversal exit: ROC={roc_f:.1f}% recovered, "
                f"RSI={rsi_f:.1f}, "
                f"regime={reg['regime']}(conf={rconf:.2f})"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "reversal",
                "signal_type": "SELL",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

    return signals
