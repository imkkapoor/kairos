"""
backtesting/strategies/bt_rsi.py — RSI mean-reversion adapter for backtesting.py.

Mirrors the signal logic in strategies/rsi.py:
  BUY:  RSI < 30 AND close < bb_lower
  SELL: RSI > 70 AND close > bb_upper

Regime is simplified to per-ticker ADX-based (no cross-sectional scanner).
Fill price is next bar's Open (backtesting.py default with exclusive_orders=True).
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


class RSIStrategy(Strategy):
    # Set to e.g. 0.99 for single-ticker mode (deploy ~100% of equity per trade).
    # When 0.0, uses ATR-based risk sizing (portfolio mode).
    invest_fraction: float = 0.0

    def init(self):
        pass  # All indicators are precomputed in the DB and loaded via load_combined()

    def next(self):
        rsi = self.data.rsi_14[-1]
        close = self.data.Close[-1]
        bb_lower = self.data.bb_lower[-1]
        bb_upper = self.data.bb_upper[-1]
        atr = self.data.atr_14[-1]
        volume = self.data.Volume[-1]
        volume_sma = self.data.volume_sma[-1]

        if any(v != v for v in (rsi, close, bb_lower, atr)):  # NaN check
            return
        if atr is None or atr == 0:
            return

        vol_mult = _clamp(volume / volume_sma, 0.5, 1.5) if (volume_sma and volume_sma > 0) else 1.0
        # Simplified per-ticker regime: no cross-sectional data available
        regime_mult = 1.0

        # --- BUY: RSI oversold + below lower Bollinger Band ---
        if rsi < 30 and close < bb_lower and not self.position:
            base = _clamp((30 - rsi) / 15, 0, 1)
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

        # --- SELL: RSI overbought + above upper Bollinger Band ---
        elif rsi > 70 and close > bb_upper:
            if self.position:
                self.position.close()
