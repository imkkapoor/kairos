"""
backtesting/strategies/bt_macd.py — MACD crossover adapter for backtesting.py.

Mirrors the signal logic in strategies/macd.py:
  BUY:  MACD line crosses above signal line AND ADX > 20
  SELL: MACD line crosses below signal line AND ADX > 20

Strength: base = abs(macd_hist) / (abs(macd_signal) + 1e-9), clamped 0–1.
Regime: simplified per-ticker ADX-based.
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


class MACDStrategy(Strategy):
    # Set to e.g. 0.99 for single-ticker mode (deploy ~100% of equity per trade).
    # When 0.0, uses ATR-based risk sizing (portfolio mode).
    invest_fraction: float = 0.0

    def init(self):
        pass  # All indicators precomputed in DB

    def next(self):
        if len(self.data) < 2:
            return

        curr_macd = self.data.macd_line[-1]
        prev_macd = self.data.macd_line[-2]
        curr_signal = self.data.macd_signal[-1]
        prev_signal = self.data.macd_signal[-2]
        macd_hist = self.data.macd_hist[-1]
        adx = self.data.adx_14[-1]
        atr = self.data.atr_14[-1]
        volume = self.data.Volume[-1]
        volume_sma = self.data.volume_sma[-1]

        if any(v != v for v in (curr_macd, prev_macd, curr_signal, prev_signal, atr, adx)):
            return
        if atr is None or atr == 0:
            return
        if adx is None or adx < 20:
            return

        vol_mult = _clamp(volume / volume_sma, 0.5, 1.5) if (volume_sma and volume_sma > 0) else 1.0
        regime_mult = 1.0  # Simplified per-ticker regime

        bullish_cross = curr_macd > curr_signal and prev_macd <= prev_signal
        bearish_cross = curr_macd < curr_signal and prev_macd >= prev_signal

        # --- BUY: MACD crosses above signal ---
        if bullish_cross and not self.position:
            base = _clamp(abs(macd_hist) / (abs(curr_signal) + 1e-9), 0, 1)
            strength = min(base * regime_mult * vol_mult, 1.0)
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

        # --- SELL: MACD crosses below signal ---
        elif bearish_cross and self.position:
            self.position.close()
