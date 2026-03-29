"""
strategies/macd.py — MACD + ADX crossover signal generator.

BUY:  macd_line crosses above macd_signal AND adx_14 > 20
SELL: macd_line crosses below macd_signal AND adx_14 > 20

Strength:
  base = abs(macd_hist) / (abs(macd_signal) + 1e-9), clamped 0.0-1.0
  regime_mult: TRENDING=1.0, CHOPPY=0.5, CRISIS=0.1
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
    "TRENDING": 1.0,
    "CHOPPY":   0.5,
    "CRISIS":   0.1,
}

_DEFAULT_REGIME = {"regime": "CHOPPY", "confidence": 0.5}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_signals(
    tickers: list[str],
    indicator_rows: dict[str, dict],
    prev_indicator_rows: dict[str, dict],
    regimes: dict[str, dict],
    open_position_tickers: list[str],
) -> list[dict]:
    """Generate MACD crossover signals for *tickers*.

    Returns a list of signal dicts ready for ``insert_signals()``.
    """
    open_set = set(open_position_tickers)
    signals: list[dict] = []

    for ticker in tickers:
        row  = indicator_rows.get(ticker)
        prev = prev_indicator_rows.get(ticker)
        if row is None or prev is None:
            continue

        macd_line   = row.get("macd_line")
        macd_signal = row.get("macd_signal")
        macd_hist   = row.get("macd_hist")
        adx         = row.get("adx_14")
        close       = row.get("close")
        volume      = row.get("volume")
        volume_sma  = row.get("volume_sma")

        # Skip if required values are missing
        if any(_nan(x) for x in [macd_line, macd_signal, adx]):
            continue
        if _nan(close) or float(close) == 0:
            continue

        macd_line_f   = float(macd_line)
        macd_signal_f = float(macd_signal)
        macd_hist_f   = float(macd_hist) if not _nan(macd_hist) else 0.0
        adx_f         = float(adx)

        prev_macd_line   = prev.get("macd_line")
        prev_macd_signal = prev.get("macd_signal")
        if any(_nan(x) for x in [prev_macd_line, prev_macd_signal]):
            continue
        prev_macd_line_f   = float(prev_macd_line)
        prev_macd_signal_f = float(prev_macd_signal)

        # ADX filter
        if adx_f <= 20:
            continue

        reg   = regimes.get(ticker, _DEFAULT_REGIME)
        rmult = _REGIME_MULT.get(reg["regime"], 1.0)
        rconf = float(reg["confidence"])
        vmult = _volume_mult(volume, volume_sma)

        interval = row.get("interval", "1d")

        # Base strength: histogram as fraction of signal line
        if _nan(macd_signal) or abs(macd_signal_f) < 1e-9:
            base = _clamp(abs(macd_hist_f), 0.0, 1.0)
        else:
            base = _clamp(abs(macd_hist_f) / (abs(macd_signal_f) + 1e-9), 0.0, 1.0)

        # BULLISH CROSSOVER: macd_line crosses above signal
        if macd_line_f > macd_signal_f and prev_macd_line_f <= prev_macd_signal_f:
            if ticker in open_set:
                continue
            final = min(base * rmult * rconf * vmult, 1.0)
            reason = (
                f"MACD({macd_line_f:.4f}) crossed above signal({macd_signal_f:.4f}), "
                f"hist={macd_hist_f:.4f}, ADX={adx_f:.1f}, "
                f"regime={reg['regime']}(conf={rconf:.2f}), "
                f"vol_mult={vmult:.2f}"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "macd",
                "signal_type": "BUY",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

        # BEARISH CROSSOVER: macd_line crosses below signal
        elif macd_line_f < macd_signal_f and prev_macd_line_f >= prev_macd_signal_f:
            final = min(base * rmult * rconf * vmult, 1.0)
            reason = (
                f"MACD({macd_line_f:.4f}) crossed below signal({macd_signal_f:.4f}), "
                f"hist={macd_hist_f:.4f}, ADX={adx_f:.1f}, "
                f"regime={reg['regime']}(conf={rconf:.2f}), "
                f"vol_mult={vmult:.2f}"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "macd",
                "signal_type": "SELL",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

    return signals
