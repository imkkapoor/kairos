"""
tests/test_backtester_fx_alignment.py — FX-rate index alignment / no-lookahead.

The backtester used to resolve the daily FX rate with
``fx_rates.get(day_ts, fx_rates.iloc[-1])``. But ``day_ts`` comes from
``price_data`` and carries the price bar's *market time* (ET-midnight →
04:00/05:00 UTC), whereas ``load_fx_rates`` indexes the series at **UTC
midnight**. So ``.get(day_ts)`` ALWAYS missed and fell back to
``fx_rates.iloc[-1]`` — the last (future) rate in the window slice. Because the
window slice has no upper bound, that was the rate at the *end of the whole
backtest range*: both a lookahead and a wrong, constant rate for every CAD
(.TO) position.

The fix keys on the calendar date at UTC midnight and uses ``Series.asof`` to
take the most recent rate at or before today (never a future rate).

Run from backend/:
    python -m unittest tests.test_backtester_fx_alignment -v
"""

import os
import re
import sys
import unittest

import pandas as pd

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_RUNNER_PATH = os.path.join(_BACKEND_DIR, "backtesting", "portfolio_runner.py")


class TestFxLookupSource(unittest.TestCase):
    """Structural guard: the buggy idiom must be gone, the fix must be present."""

    def setUp(self):
        with open(_RUNNER_PATH, "r") as f:
            self.source = f.read()

    def test_no_direct_get_on_day_ts(self):
        # The lookahead bug was fx_rates.get(day_ts, ...). It must not return.
        self.assertNotRegex(
            self.source,
            re.compile(r"fx_rates\.get\(\s*day_ts"),
            "portfolio_runner.py must not resolve FX with fx_rates.get(day_ts, ...) "
            "— day_ts is at market time (04:00/05:00 UTC) and always misses the "
            "midnight-indexed FX series (lookahead bug).",
        )

    def test_uses_asof(self):
        self.assertIn(
            "fx_rates.asof(",
            self.source,
            "portfolio_runner.py must resolve FX with fx_rates.asof(midnight ts).",
        )

    def test_no_iloc_minus_one_fx_fallback(self):
        # The old fallbacks used fx_rates.iloc[-1] (future rate). Neither the
        # per-day lookup nor the end-of-window close should use it now.
        self.assertNotIn(
            "fx_rates.iloc[-1]",
            self.source,
            "fx_rates.iloc[-1] is a future rate — use .asof(day) with an "
            "iloc[0] fallback instead.",
        )


class TestFxAsofSemantics(unittest.TestCase):
    """Functional check of the asof idiom the runner now uses."""

    @staticmethod
    def _resolve(fx_rates: pd.Series, day_ts: pd.Timestamp) -> float:
        # Mirror the inline resolution in portfolio_runner.run_window.
        val = fx_rates.asof(pd.Timestamp(day_ts.date(), tz="UTC"))
        return float(val) if pd.notna(val) else float(fx_rates.iloc[0])

    def _series(self):
        # FX series as load_fx_rates builds it: UTC-midnight index, one per day.
        idx = pd.date_range("2020-01-01", "2020-01-05", freq="D", tz="UTC")
        return pd.Series([1.0, 1.1, 1.2, 1.3, 1.4], index=idx)

    def test_resolves_todays_rate_not_future(self):
        fx = self._series()
        # day_ts carries market time (05:00 UTC) — the bug's trigger.
        day_ts = pd.Timestamp("2020-01-02 05:00:00", tz="UTC")
        self.assertEqual(self._resolve(fx, day_ts), 1.1)   # today's rate
        self.assertNotEqual(self._resolve(fx, day_ts), 1.4)  # NOT the last/future rate

    def test_uses_prior_rate_across_gap(self):
        # Weekend/holiday gap: must carry the most recent PRIOR rate, not the next.
        idx = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-06"]).tz_localize("UTC")
        fx = pd.Series([1.0, 1.1, 1.6], index=idx)
        day_ts = pd.Timestamp("2020-01-04 05:00:00", tz="UTC")
        self.assertEqual(self._resolve(fx, day_ts), 1.1)  # prior, never the 1.6 future

    def test_before_series_start_falls_back_to_first(self):
        fx = self._series()
        day_ts = pd.Timestamp("2019-12-30 05:00:00", tz="UTC")
        self.assertEqual(self._resolve(fx, day_ts), 1.0)  # iloc[0] fallback, not NaN


if __name__ == "__main__":
    unittest.main(verbosity=2)
