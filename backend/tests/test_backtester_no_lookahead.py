"""
tests/test_backtester_no_lookahead.py — [C2] Backtester sizing lookahead fix.

The backtester used to size trades at day T's Open using day T's Close prices
(`portfolio.get_total_value(close_prices, fx_today)`). That's forward-looking:
today's Close is unknown at today's Open fill time. The fix threads a separate
``sizing_prices`` dict — populated from yesterday's Close (or today's Open on
day 0 of the window) — through the sizing / exposure / can_open block.

This test is structural: it enforces that the sizing block at lines ~860–920
of ``backtesting/portfolio_runner.py`` no longer contains references to
``close_prices``. End-of-day valuation, trailing-stop ratchet, and daily
snapshots are legitimate uses of ``close_prices`` and stay unchanged.

Run from backend/:
    python -m unittest tests.test_backtester_no_lookahead -v
"""

import os
import re
import sys
import unittest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_RUNNER_PATH = os.path.join(_BACKEND_DIR, "backtesting", "portfolio_runner.py")


class TestSizingBlockUsesPrevClose(unittest.TestCase):

    def setUp(self):
        with open(_RUNNER_PATH, "r") as f:
            self.source = f.read()

    def test_sizing_prices_dict_is_defined(self):
        # The fix introduces a sizing_prices dict.
        self.assertIn(
            "sizing_prices",
            self.source,
            "portfolio_runner.py must define sizing_prices for the sizing block.",
        )

    def test_prev_close_prices_advances_each_day(self):
        # prev_close_prices must be updated at the end of each day loop from today's close.
        pattern = re.compile(r"prev_close_prices\s*=\s*dict\(close_prices\)")
        self.assertRegex(
            self.source,
            pattern,
            "prev_close_prices must be advanced to close_prices at end of day.",
        )

    def test_sizing_block_calls_use_sizing_prices(self):
        # The three critical calls inside the sizing block must use sizing_prices,
        # not close_prices:
        #   portfolio.get_total_value(...)   → total_val for dollar_risk/max_position
        #   sum(pos["qty"] * ...(t))         → invested for total_exposure cap
        #   portfolio.can_open(..., prices)  → final sanity check
        for expected in (
            "portfolio.get_total_value(sizing_prices",
            "sizing_prices.get(t, 0.0)",
            "portfolio.can_open(\n                tkr, cost_usd, sector, sizing_prices",
        ):
            self.assertIn(
                expected,
                self.source,
                f"Missing expected sizing-block usage: {expected!r}",
            )

    def test_no_close_prices_reference_in_sizing_block(self):
        # Extract the sizing block (from the sizing_prices dict-comp through the
        # portfolio.can_open call). Assert BARE close_prices (not prev_close_prices
        # or sizing_prices) is not referenced within.
        m = re.search(
            r"sizing_prices: dict\[str, float\] = \{.*?portfolio\.can_open\([^)]*\)",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "Could not locate the sizing block for structural check.",
        )
        sizing_block = m.group(0)
        # Word-boundary match: matches "close_prices" but not "prev_close_prices"
        # or "sizing_prices".
        bare_close = re.compile(r"(?<![a-zA-Z_])close_prices\b")
        self.assertIsNone(
            bare_close.search(sizing_block),
            "Sizing block must not reference bare close_prices — that's the "
            "lookahead bug [C2].",
        )

    def test_close_prices_still_used_for_end_of_day(self):
        # Trailing-stop ratchet and end-of-day equity valuation legitimately
        # use close_prices. Guard against accidental over-refactor.
        self.assertIn(
            "close_px = close_prices.get(ticker)",
            self.source,
            "Trailing-stop ratchet must still read close_prices (end-of-day).",
        )
        self.assertIn(
            "total_val = portfolio.get_total_value(close_prices, fx_today)",
            self.source,
            "End-of-day equity valuation must still use close_prices.",
        )


class TestSizingPricesFallback(unittest.TestCase):
    """Simulate the sizing_prices dict-comp in isolation."""

    @staticmethod
    def _build_sizing_prices(prev_close_prices: dict, open_prices: dict) -> dict:
        # Mirror the inline dict-comprehension in portfolio_runner.py.
        return {
            t: prev_close_prices.get(t, open_prices.get(t, 0.0))
            for t in set(prev_close_prices) | set(open_prices)
        }

    def test_prev_close_wins_when_both_present(self):
        sizing = self._build_sizing_prices(
            prev_close_prices={"AAPL": 100.0},
            open_prices={"AAPL": 110.0},
        )
        # Sizing must use YESTERDAY's close (100), not today's open (110).
        self.assertEqual(sizing["AAPL"], 100.0)

    def test_open_used_when_no_prev_close(self):
        # Day 0 of the window — prev_close_prices is empty. Fall back to today's Open
        # (knowable at 9:31 ET).
        sizing = self._build_sizing_prices(
            prev_close_prices={},
            open_prices={"AAPL": 110.0},
        )
        self.assertEqual(sizing["AAPL"], 110.0)

    def test_union_of_tickers_present(self):
        # A ticker that traded yesterday but not today should still be valued.
        sizing = self._build_sizing_prices(
            prev_close_prices={"AAPL": 100.0, "SHOP.TO": 200.0},
            open_prices={"AAPL": 110.0},
        )
        self.assertIn("SHOP.TO", sizing)
        self.assertEqual(sizing["SHOP.TO"], 200.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
