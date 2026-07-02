"""
tests/test_vix_lookahead.py — [C3] Remove same-day VIX lookahead.

Every backtester VIX lookup used to include day T's VIX close when deciding
sizing at day T's 9:31 ET Open. The fix changes all slices from
``vix_series.index <= target_ts`` to ``vix_series.index < target_ts`` and
removes the exact-day match branch in ``get_vix_regime``.

Run from backend/:
    python -m unittest tests.test_vix_lookahead -v
"""

import os
import re
import sys
import unittest
from datetime import date

import pandas as pd

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from strategies.vol_regime import get_vix_regime, is_vix_spike


def _build_series(entries: list[tuple[str, float]]) -> pd.Series:
    """Build a VIX series indexed at UTC midnight from (date_str, vix)."""
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d, _ in entries])
    vals = [v for _, v in entries]
    return pd.Series(vals, index=idx)


class TestGetVixRegimeNoSameDay(unittest.TestCase):
    """Day T's VIX close must not influence get_vix_regime(day_T)."""

    def test_extreme_on_day_t_is_ignored(self):
        # Day T-1 = 15 (NORMAL), day T = 45 (EXTREME).
        # Under the OLD bug: get_vix_regime(day_T) returned EXTREME.
        # Under the fix: returns NORMAL (T-1's value).
        series = _build_series([("2026-04-01", 15.0), ("2026-04-02", 45.0)])
        result = get_vix_regime(date(2026, 4, 2), series)

        self.assertEqual(result["vix"], 15.0)
        self.assertEqual(result["regime"], "NORMAL")
        self.assertEqual(result["size_mult"], 1.0)

    def test_no_prior_data_falls_open(self):
        # Only day T is in the series → no prior data → NORMAL fail-open with vix=None.
        series = _build_series([("2026-04-02", 45.0)])
        result = get_vix_regime(date(2026, 4, 2), series)

        self.assertIsNone(result["vix"])
        self.assertEqual(result["regime"], "NORMAL")

    def test_uses_most_recent_prior_bar(self):
        # Weekend fallback: series has Fri and Mon; look up on Mon.
        # Should return Mon's PRIOR — Friday's — value.
        series = _build_series([
            ("2026-04-03", 32.0),   # Friday (HIGH)
            ("2026-04-06", 12.0),   # Monday (would be NORMAL if used)
        ])
        result = get_vix_regime(date(2026, 4, 6), series)

        self.assertEqual(result["vix"], 32.0)
        self.assertEqual(result["regime"], "HIGH")

    def test_empty_series_returns_normal(self):
        series = pd.Series([], index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
        result = get_vix_regime(date(2026, 4, 6), series)
        self.assertEqual(result["regime"], "NORMAL")
        self.assertIsNone(result["vix"])


class TestIsVixSpikeNoSameDay(unittest.TestCase):
    """Day T's spike must not fire at day T's Open."""

    def test_spike_at_day_t_does_not_fire(self):
        # 10-day flat SMA of 15, then day T = 30 (100% spike over sma).
        # Under the OLD bug: is_vix_spike(day_T) returned True.
        # Under the fix: returns False (uses day T-1 as "today", flat = no spike).
        dates = [f"2026-03-{d:02d}" for d in range(2, 12)] + ["2026-03-12"]
        vals = [15.0] * 10 + [30.0]  # 10 flat days + spike on day 11
        series = _build_series(list(zip(dates, vals)))

        # Asking about day T = 2026-03-12 (the spike day) — must NOT fire.
        self.assertFalse(is_vix_spike(series, date(2026, 3, 12)))

    def test_spike_at_day_t_minus_1_fires_on_day_t(self):
        # If day T-1 had the spike, asking about day T should fire — that's
        # the correct causal chain.
        dates = [f"2026-03-{d:02d}" for d in range(2, 12)] + ["2026-03-13"]
        vals = [15.0] * 9 + [30.0, 999.0]  # spike on day 10, arbitrary day 11
        series = _build_series(list(zip(dates, vals)))

        # Day T = 2026-03-13. "Today" from the slice is day T-1 = 2026-03-11 = 30.
        # SMA of last 10 prior values: [15,15,15,15,15,15,15,15,15,30] = 16.5
        # 30 > 16.5 * 1.2 = 19.8 → spike fires.
        self.assertTrue(is_vix_spike(series, date(2026, 3, 13)))

    def test_returns_false_with_insufficient_history(self):
        # Fewer than sma_window (default 10) PRIOR bars → False.
        series = _build_series([
            ("2026-03-02", 15.0), ("2026-03-03", 45.0),  # only 2 bars
        ])
        self.assertFalse(is_vix_spike(series, date(2026, 3, 4)))


class TestVIXSourceCodeStrictLessThan(unittest.TestCase):
    """Belt-and-braces: no ``<= target_ts`` slice may reappear."""

    def _read(self, rel_path: str) -> str:
        with open(os.path.join(_BACKEND_DIR, rel_path), "r") as f:
            return f.read()

    def test_vol_regime_uses_strict_lt(self):
        src = self._read("strategies/vol_regime.py")
        self.assertNotRegex(
            src,
            r"vix_series\[vix_series\.index <= target_ts\]",
            "strategies/vol_regime.py must not slice `<= target_ts` — that "
            "reintroduces the same-day VIX lookahead [C3].",
        )
        # The exact-day match branch must also be gone.
        self.assertNotIn(
            "if target_ts in vix_series.index:",
            src,
            "get_vix_regime must not use the exact-day match branch — day T's "
            "close is not knowable at day T's 9:31 ET Open.",
        )

    def test_portfolio_runner_uses_strict_lt(self):
        src = self._read("backtesting/portfolio_runner.py")
        # Two spots in portfolio_runner used <= _vix_ts.
        self.assertNotRegex(
            src,
            r"vix_series\[vix_series\.index <= _vix_ts\]",
            "backtesting/portfolio_runner.py must not slice `<= _vix_ts` [C3].",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
