"""
backtesting/strategies/bt_sector_rotation.py — Sector rotation adapter for backtesting.py.

NOTE: The live sector rotation strategy requires simultaneous access to
SPY, XLE, XLK, TLT, XLU, XLV data alongside the primary ticker to compute
macro regime scores and sector favorability.

For single-ticker backtesting: this strategy is SKIPPED (returns no signals).
Full sector rotation is only meaningful in portfolio_runner.py where all
sector ETF data can be loaded simultaneously.
"""

from backtesting._lib import Strategy


class SectorRotationStrategy(Strategy):
    """Placeholder — sector rotation is skipped in single-ticker backtests.

    Sector rotation requires cross-sectional data (multiple ETFs) which is
    not available in backtesting.py's single-ticker framework.
    See portfolio_runner.py for the full implementation.
    """

    def init(self):
        pass

    def next(self):
        # No signals generated in single-ticker mode
        pass
