"""
backtesting/strategies/bt_reversal.py — Short-term reversal adapter for backtesting.py.

Mirrors the signal logic in strategies/reversal.py.

NOTE: The live reversal strategy requires ranking ALL tickers by ROC simultaneously
(cross-sectional). For single-ticker backtesting this is approximated:
  BUY:  roc_20 < -5 AND rsi_14 > 20  (proxy for bottom-decile condition)
  EXIT: after 20 bars OR roc_20 > 5   (recovery exit)
Full cross-sectional ranking is only available in portfolio_runner.py.
"""

from backtesting._lib import Strategy

from backtesting.config import (
    ATR_MULTIPLIER,
    INITIAL_CAPITAL,
    MAX_PORTFOLIO_RISK,
    MAX_POSITION_SIZE,
    MIN_SIGNAL_STRENGTH,
    TAKE_PROFIT_ATR_MULT,
)


def _clamp(value, lo, hi):
    return max(lo, min(value, hi))


class ReversalStrategy(Strategy):
    # Set to e.g. 0.99 for single-ticker mode (deploy ~100% of equity per trade).
    # When 0.0, uses ATR-based risk sizing (portfolio mode).
    invest_fraction: float = 0.0

    def init(self):
        self._bars_held = 0

    def next(self):
        roc = self.data.roc_20[-1]
        rsi = self.data.rsi_14[-1]
        atr = self.data.atr_14[-1]
        volume = self.data.Volume[-1]
        volume_sma = self.data.volume_sma[-1]

        if any(v != v for v in (roc, rsi, atr)):
            return
        if atr is None or atr == 0:
            return

        vol_mult = _clamp(volume / volume_sma, 0.5, 1.5) if (volume_sma and volume_sma > 0) else 1.0

        # --- Track bars held for time-based exit ---
        if self.position:
            self._bars_held += 1

            # EXIT: time-based (20 bars) or recovery (ROC > 5)
            if self._bars_held >= 20 or roc > 5:
                self.position.close()
                self._bars_held = 0
            return

        # --- BUY: approximated bottom-decile condition ---
        # Proxy for cross-sectional ranking: roc_20 < -5 means deep underperformer
        if roc < -5 and rsi > 20:
            base = _clamp(abs(roc) / 20, 0, 1)
            strength = min(base * vol_mult, 1.0)
            if strength < MIN_SIGNAL_STRENGTH:
                return

            entry_price = self.data.Close[-1]
            stop_price = entry_price - (ATR_MULTIPLIER * atr)
            risk_per_share = entry_price - stop_price
            if risk_per_share <= 0:
                return

            take_profit = entry_price + (TAKE_PROFIT_ATR_MULT * atr)

            if self.invest_fraction > 0:
                # Single-ticker mode: deploy invest_fraction of equity (e.g. 0.99)
                self.buy(size=self.invest_fraction, sl=stop_price, tp=take_profit)
            else:
                dollar_risk = strength * MAX_PORTFOLIO_RISK * self.equity
                size = int(dollar_risk / risk_per_share)
                if size < 1:
                    return
                if size * entry_price > MAX_POSITION_SIZE * self.equity:
                    size = int((MAX_POSITION_SIZE * self.equity) / entry_price)
                if size < 1:
                    return
                self.buy(size=size, sl=stop_price, tp=take_profit)
            self._bars_held = 0
