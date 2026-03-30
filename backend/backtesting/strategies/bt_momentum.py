"""
backtesting/strategies/bt_momentum.py — MA crossover / trend-following adapter.

Mirrors the signal logic in strategies/momentum.py:
  BUY:  Golden cross (MA50 crosses above MA200) or continuation (close > MA50, ADX > 25)
  SELL: Death cross (MA50 crosses below MA200) or exit (close < MA50 while trending up)

ROC bonus: +0.10 if ROC > 10%, +0.05 if ROC > 5%, -0.05 if ROC < -5%.
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


class MomentumStrategy(Strategy):
    # Set to e.g. 0.99 for single-ticker mode (deploy ~100% of equity per trade).
    # When 0.0, uses ATR-based risk sizing (portfolio mode).
    invest_fraction: float = 0.0

    def init(self):
        pass  # All indicators precomputed in DB

    def next(self):
        if len(self.data) < 2:
            return

        curr_ma50 = self.data.ma_50[-1]
        prev_ma50 = self.data.ma_50[-2]
        curr_ma200 = self.data.ma_200[-1]
        prev_ma200 = self.data.ma_200[-2]
        close = self.data.Close[-1]
        atr = self.data.atr_14[-1]
        adx = self.data.adx_14[-1]
        roc = self.data.roc_20[-1]
        volume = self.data.Volume[-1]
        volume_sma = self.data.volume_sma[-1]

        if any(v != v for v in (curr_ma50, prev_ma50, curr_ma200, prev_ma200, atr)):
            return
        if atr is None or atr == 0:
            return

        vol_mult = _clamp(volume / volume_sma, 0.5, 1.5) if (volume_sma and volume_sma > 0) else 1.0
        regime_mult = 1.0  # Simplified per-ticker regime

        golden_cross = curr_ma50 > curr_ma200 and prev_ma50 <= prev_ma200
        death_cross = curr_ma50 < curr_ma200 and prev_ma50 >= prev_ma200

        # --- BUY: Golden cross ---
        if golden_cross and not self.position:
            gap_pct = abs(curr_ma50 - curr_ma200) / curr_ma200 if curr_ma200 > 0 else 0
            base = _clamp(gap_pct * 10, 0.3, 1.0)
            strength = min(base * regime_mult * vol_mult, 1.0)

            # ROC bonus
            if roc == roc:  # NaN check
                if roc > 10:
                    strength = min(strength + 0.10, 1.0)
                elif roc > 5:
                    strength = min(strength + 0.05, 1.0)
                elif roc < -5:
                    strength = max(strength - 0.05, 0.0)

            if strength < MIN_SIGNAL_STRENGTH:
                return
            self._enter_long(strength, atr)

        # --- BUY: Continuation (trending up, close > MA50, strong ADX) ---
        elif (not self.position and curr_ma50 > curr_ma200
              and close > curr_ma50 and adx == adx and adx > 25):
            base = _clamp(adx / 40, 0, 0.5)
            strength = min(base * regime_mult * vol_mult, 1.0)

            if roc == roc:
                if roc > 10:
                    strength = min(strength + 0.10, 1.0)
                elif roc > 5:
                    strength = min(strength + 0.05, 1.0)
                elif roc < -5:
                    strength = max(strength - 0.05, 0.0)

            if strength < MIN_SIGNAL_STRENGTH:
                return
            self._enter_long(strength, atr)

        # --- SELL: Death cross ---
        elif death_cross and self.position:
            self.position.close()

        # --- SELL: Exit (close dropped below MA50 while in uptrend) ---
        elif self.position and close < curr_ma50 and curr_ma50 > curr_ma200:
            self.position.close()

    def _enter_long(self, strength: float, atr: float):
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
