"""
tests/test_portfolio_close_fx.py — [C1] FX-at-close in live Portfolio.

Live ``Portfolio.close_position`` used to multiply by the FX rate stored
*at open*, silently discarding all FX P&L between open and close. The fix
threads the *current* FX rate through the close, so realised P&L and the
cash credit match the mark-to-market value just before close.

Run from backend/:
    python -m unittest tests.test_portfolio_close_fx -v
"""

import os
import sys
import unittest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _fresh_portfolio(base_currency: str = "CAD", initial: float = 100_000.0):
    """Build a Portfolio with the requested base currency + starting cash.

    Portfolio reads PORTFOLIO_CURRENCY / INITIAL_CAPITAL from env at __init__
    time, so we override the env vars before instantiating.
    """
    os.environ["PORTFOLIO_CURRENCY"] = base_currency
    os.environ["INITIAL_CAPITAL"] = str(initial)
    from simulator.portfolio import Portfolio  # local import so env applies
    return Portfolio(cash=initial)


class TestCloseUsesCurrentFX(unittest.TestCase):
    """Realised P&L and cash credit must use the FX rate at CLOSE, not at open."""

    def test_cad_base_usd_position_fx_up(self):
        # CAD-base portfolio. Open USD stock at USDCAD=1.30, close at 1.40.
        # Native P&L = (110-100)*10 = $100 USD.
        # Under the fix: realised P&L = 100*1.40 = $140 CAD.
        # Under the OLD bug: realised P&L = 100*1.30 = $130 CAD (missing $10).
        p = _fresh_portfolio("CAD")
        cash_before = p.cash

        p.open_position(
            ticker="AAPL", qty=10, price=100.0,
            stop_loss=95.0, take_profit=115.0,
            strategy="rsi", sector="Tech",
            currency="USD", fx_rate=1.30,
        )

        # Cash after open: cash_before - 100*10*1.30 = cash_before - 1300
        self.assertAlmostEqual(p.cash, cash_before - 1300.0, places=6)

        # Close at exit=110, current fx=1.40
        realised = p.close_position("AAPL", exit_price=110.0, fx_rate=1.40)

        # Realised P&L must include the FX gain.
        self.assertAlmostEqual(realised, 140.0, places=6)
        # Cash credit = 110 * 10 * 1.40 = 1540. Net delta = 1540 - 1300 = 240.
        self.assertAlmostEqual(p.cash, cash_before + 240.0, places=6)

    def test_cad_base_usd_position_fx_down(self):
        # Same setup but FX moves against us: open 1.30, close 1.20.
        # Native P&L = $100 USD.
        # Under the fix: realised P&L = 100*1.20 = $120 CAD.
        p = _fresh_portfolio("CAD")
        cash_before = p.cash

        p.open_position(
            ticker="AAPL", qty=10, price=100.0,
            stop_loss=95.0, take_profit=115.0,
            strategy="rsi", sector="Tech",
            currency="USD", fx_rate=1.30,
        )

        realised = p.close_position("AAPL", exit_price=110.0, fx_rate=1.20)

        self.assertAlmostEqual(realised, 120.0, places=6)
        # Cash: -1300 (open) + 110*10*1.20 (close) = -1300 + 1320 = +20 net
        self.assertAlmostEqual(p.cash, cash_before + 20.0, places=6)

    def test_usd_only_position_unchanged(self):
        # Regression: USD-only case (fx_rate=1.0 throughout) must produce the same
        # numbers as before the fix.
        p = _fresh_portfolio("USD")
        cash_before = p.cash

        p.open_position(
            ticker="AAPL", qty=10, price=100.0,
            stop_loss=95.0, take_profit=115.0,
            strategy="rsi", sector="Tech",
            currency="USD", fx_rate=1.0,
        )
        realised = p.close_position("AAPL", exit_price=110.0, fx_rate=1.0)

        self.assertAlmostEqual(realised, 100.0, places=6)
        self.assertAlmostEqual(p.cash, cash_before + 100.0, places=6)

    def test_omitted_fx_falls_back_to_open_rate(self):
        # Backwards compatibility: if a caller doesn't pass fx_rate, use the
        # stored open-time rate. Preserves the OLD behaviour for any code path
        # not yet updated.
        p = _fresh_portfolio("CAD")

        p.open_position(
            ticker="AAPL", qty=10, price=100.0,
            stop_loss=95.0, take_profit=115.0,
            strategy="rsi", sector="Tech",
            currency="USD", fx_rate=1.30,
        )
        realised = p.close_position("AAPL", exit_price=110.0)  # no fx_rate

        # Fallback path: uses pos["fx_rate"] = 1.30
        self.assertAlmostEqual(realised, 130.0, places=6)

    def test_invariant_cash_delta_matches_exit_times_fx(self):
        # After open+close, cash delta should equal exit*qty*fx_close - open*qty*fx_open.
        # Realised P&L should equal that same delta.
        p = _fresh_portfolio("CAD")
        cash_before = p.cash

        p.open_position(
            ticker="SHOP.TO", qty=5, price=200.0,
            stop_loss=190.0, take_profit=220.0,
            strategy="momentum", sector="Tech",
            currency="CAD", fx_rate=1.0,  # native CAD in CAD base
        )
        realised = p.close_position("SHOP.TO", exit_price=210.0, fx_rate=1.0)

        cash_delta_expected = 210.0 * 5 * 1.0 - 200.0 * 5 * 1.0
        self.assertAlmostEqual(realised, cash_delta_expected, places=6)
        self.assertAlmostEqual(p.cash - cash_before, cash_delta_expected, places=6)


class TestCloseUnknownTicker(unittest.TestCase):
    def test_close_unknown_returns_zero(self):
        p = _fresh_portfolio("CAD")
        cash_before = p.cash
        realised = p.close_position("ZZZZ", exit_price=100.0, fx_rate=1.30)
        self.assertEqual(realised, 0.0)
        self.assertEqual(p.cash, cash_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
