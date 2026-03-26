"""
strategies/rsi.py — RSI mean-reversion signal generator.

BUY signal:  RSI < 30 AND close < bb_lower
             skip if ticker is already an open position
SELL signal: RSI > 70 AND close > bb_upper

Strength calculation:
  base_strength  BUY  = clamp((30 - rsi) / 15, 0, 1)
  base_strength  SELL = clamp((rsi - 70) / 15, 0, 1)
  regime_mult:   CHOPPY=1.0, TRENDING=0.4, CRISIS=0.1
  volume_mult:   clamp(volume / volume_sma, 0.5, 1.5); 1.0 if volume_sma=0/None
  final_strength = min(base * regime_mult * regime_conf * volume_mult, 1.0)

Stateless — no DB access. All inputs passed as arguments.
"""

import math
from typing import Optional


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
    "TRENDING": 0.4,
    "CRISIS":   0.1,
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
) -> list[dict]:
    """Generate RSI mean-reversion signals for *tickers*.

    Returns a list of signal dicts ready for ``insert_signals()``:
      {ticker, interval, strategy, signal_type, strength, reason}
    """
    open_set = set(open_position_tickers)
    signals: list[dict] = []

    for ticker in tickers:
        row = indicator_rows.get(ticker)
        if row is None:
            continue

        rsi        = row.get("rsi_14")
        close      = row.get("close")
        bb_lower   = row.get("bb_lower")
        bb_upper   = row.get("bb_upper")
        volume     = row.get("volume")
        volume_sma = row.get("volume_sma")

        # Must have complete data
        if any(_nan(x) for x in [rsi, close, bb_lower, bb_upper]):
            continue

        rsi   = float(rsi)
        close = float(close)
        bb_lower = float(bb_lower)
        bb_upper = float(bb_upper)

        reg  = regimes.get(ticker, _DEFAULT_REGIME)
        rmult = _REGIME_MULT.get(reg["regime"], 1.0)
        rconf = float(reg["confidence"])
        vmult = _volume_mult(volume, volume_sma)

        # --- BUY ---
        if rsi < 30 and close < bb_lower:
            if ticker in open_set:
                continue  # already long, skip

            base   = _clamp((30 - rsi) / 15, 0.0, 1.0)
            final  = min(base * rmult * rconf * vmult, 1.0)
            reason = (
                f"RSI={rsi:.1f}<30, close={close:.2f}<bb_lower={bb_lower:.2f}, "
                f"regime={reg['regime']}(conf={rconf:.2f}), "
                f"vol_mult={vmult:.2f}"
            )
            signals.append({
                "ticker":   ticker,
                "interval": row.get("interval", "1d"),
                "strategy": "rsi",
                "signal_type": "BUY",
                "strength": round(final, 4),
                "reason":   reason,
                "regime":   reg["regime"],
                "regime_confidence": rconf,
            })

        # --- SELL ---
        elif rsi > 70 and close > bb_upper:
            base   = _clamp((rsi - 70) / 15, 0.0, 1.0)
            final  = min(base * rmult * rconf * vmult, 1.0)
            reason = (
                f"RSI={rsi:.1f}>70, close={close:.2f}>bb_upper={bb_upper:.2f}, "
                f"regime={reg['regime']}(conf={rconf:.2f}), "
                f"vol_mult={vmult:.2f}"
            )
            signals.append({
                "ticker":   ticker,
                "interval": row.get("interval", "1d"),
                "strategy": "rsi",
                "signal_type": "SELL",
                "strength": round(final, 4),
                "reason":   reason,
                "regime":   reg["regime"],
                "regime_confidence": rconf,
            })

    return signals
