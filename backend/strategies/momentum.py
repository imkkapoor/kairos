"""
strategies/momentum.py — Moving-average crossover / trend-following signal generator.

Signal types:
  GOLDEN CROSS  BUY  — today ma50 > ma200 AND prev ma50 <= ma200
  CONTINUATION  BUY  — ma50 > ma200 AND close > ma50 AND adx > 25
                        (ALWAYS skipped if ticker is an open position)
  DEATH CROSS   SELL — today ma50 < ma200 AND prev ma50 >= ma200
  EXIT          SELL — close < ma50 AND ma50 > ma200

Strength calculation:
  base_strength crossover (golden/death) = min(|ma50 - ma200| / close * 100, 1.0)
    ×100 factor: a 1% gap → strength 1.0
  base_strength continuation / exit      = min(adx / 40, 0.5)
  regime_mult: TRENDING=1.0, CHOPPY=0.3, CRISIS=0.1
  volume_mult: clamp(volume / volume_sma, 0.5, 1.5); 1.0 if volume_sma=0/None
  final_strength = min(base * regime_mult * regime_conf * volume_mult, 1.0)

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
    "CHOPPY":   0.3,
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
    """Generate momentum / crossover signals for *tickers*.

    Returns a list of signal dicts ready for ``insert_signals()``:
      {ticker, interval, strategy, signal_type, strength, reason}
    """
    open_set = set(open_position_tickers)
    signals: list[dict] = []

    for ticker in tickers:
        row  = indicator_rows.get(ticker)
        prev = prev_indicator_rows.get(ticker)
        if row is None:
            continue

        ma50  = row.get("ma_50")
        ma200 = row.get("ma_200")
        close = row.get("close")
        adx   = row.get("adx_14")
        volume     = row.get("volume")
        volume_sma = row.get("volume_sma")

        if any(_nan(x) for x in [ma50, ma200, close]):
            continue

        ma50  = float(ma50)
        ma200 = float(ma200)
        close = float(close)

        reg   = regimes.get(ticker, _DEFAULT_REGIME)
        rmult = _REGIME_MULT.get(reg["regime"], 1.0)
        rconf = float(reg["confidence"])
        vmult = _volume_mult(volume, volume_sma)

        interval = row.get("interval", "1d")

        # Previous bar values (may be absent)
        prev_ma50  = None if (prev is None or _nan(prev.get("ma_50")))  else float(prev["ma_50"])
        prev_ma200 = None if (prev is None or _nan(prev.get("ma_200"))) else float(prev["ma_200"])
        adx_f      = None if _nan(adx) else float(adx)

        # ----------------------------------------------------------------
        # CROSSOVERS (require previous bar)
        # ----------------------------------------------------------------
        if prev_ma50 is not None and prev_ma200 is not None:
            if close == 0:
                continue
            gap_pct = abs(ma50 - ma200) / close * 100  # ×100: 1% gap → 1.0

            # GOLDEN CROSS BUY
            if ma50 > ma200 and prev_ma50 <= prev_ma200:
                if ticker not in open_set:
                    base   = min(gap_pct, 1.0)
                    final  = min(base * rmult * rconf * vmult, 1.0)
                    roc = row.get('roc_20')
                    roc_str = ""
                    if roc is not None:
                        roc = float(roc)
                        if roc > 10:
                            final = min(final + 0.10, 1.0)
                        elif roc > 5:
                            final = min(final + 0.05, 1.0)
                        elif roc < -5:
                            final = max(final - 0.05, 0.0)
                        roc_str = f", ROC={roc:.1f}%"
                    reason = (
                        f"Golden cross: ma50={ma50:.2f}>ma200={ma200:.2f} "
                        f"(prev ma50={prev_ma50:.2f}<=ma200={prev_ma200:.2f}), "
                        f"gap={gap_pct:.2f}%, "
                        f"regime={reg['regime']}(conf={rconf:.2f}), "
                        f"vol_mult={vmult:.2f}"
                        f"{roc_str}"
                    )
                    signals.append({
                        "ticker":      ticker,
                        "interval":    interval,
                        "strategy":    "momentum",
                        "signal_type": "BUY",
                        "strength":    round(final, 4),
                        "reason":      reason,
                        "regime":      reg["regime"],
                        "regime_confidence": rconf,
                    })
                continue  # don't stack additional signals on same bar

            # DEATH CROSS SELL
            if ma50 < ma200 and prev_ma50 >= prev_ma200:
                base   = min(gap_pct, 1.0)
                final  = min(base * rmult * rconf * vmult, 1.0)
                reason = (
                    f"Death cross: ma50={ma50:.2f}<ma200={ma200:.2f} "
                    f"(prev ma50={prev_ma50:.2f}>=ma200={prev_ma200:.2f}), "
                    f"gap={gap_pct:.2f}%, "
                    f"regime={reg['regime']}(conf={rconf:.2f}), "
                    f"vol_mult={vmult:.2f}"
                )
                signals.append({
                    "ticker":      ticker,
                    "interval":    interval,
                    "strategy":    "momentum",
                    "signal_type": "SELL",
                    "strength":    round(final, 4),
                    "reason":      reason,
                    "regime":      reg["regime"],
                    "regime_confidence": rconf,
                })
                continue

        # ----------------------------------------------------------------
        # NON-CROSSOVER (continuation / exit)
        # ----------------------------------------------------------------

        # CONTINUATION BUY — always skip open positions
        if (
            ticker not in open_set
            and ma50 > ma200
            and close > ma50
            and adx_f is not None
            and adx_f > 25
        ):
            base   = min(adx_f / 40, 0.5)
            final  = min(base * rmult * rconf * vmult, 1.0)
            roc = row.get('roc_20')
            roc_str = ""
            if roc is not None:
                roc = float(roc)
                if roc > 10:
                    final = min(final + 0.10, 1.0)
                elif roc > 5:
                    final = min(final + 0.05, 1.0)
                elif roc < -5:
                    final = max(final - 0.05, 0.0)
                roc_str = f", ROC={roc:.1f}%"
            reason = (
                f"Trend continuation: close={close:.2f}>ma50={ma50:.2f}>ma200={ma200:.2f}, "
                f"ADX={adx_f:.1f}, "
                f"regime={reg['regime']}(conf={rconf:.2f}), "
                f"vol_mult={vmult:.2f}"
                f"{roc_str}"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "momentum",
                "signal_type": "BUY",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })
            continue

        # EXIT SELL — only fire when we actually hold this ticker
        if ticker in open_set and close < ma50 and ma50 > ma200:
            base    = min(adx_f / 40, 0.5) if adx_f is not None else 0.25
            final   = min(base * rmult * rconf * vmult, 1.0)
            adx_str = f"{adx_f:.1f}" if adx_f is not None else "N/A"
            reason = (
                f"Exit signal: close={close:.2f}<ma50={ma50:.2f}, "
                f"ma50>ma200={ma200:.2f}, "
                f"ADX={adx_str}, "
                f"regime={reg['regime']}(conf={rconf:.2f}), "
                f"vol_mult={vmult:.2f}"
            )
            signals.append({
                "ticker":      ticker,
                "interval":    interval,
                "strategy":    "momentum",
                "signal_type": "SELL",
                "strength":    round(final, 4),
                "reason":      reason,
                "regime":      reg["regime"],
                "regime_confidence": rconf,
            })

    return signals
