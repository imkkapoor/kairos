"""
tests/test_fetch_vix.py — VIX fetch timezone harmonisation [M4].

Run from backend/:
    python -m unittest tests.test_fetch_vix -v

The bug fix: data/fetch_vix.py used to tz_localize naive yfinance timestamps
as UTC. data/fetcher.py treats them as America/New_York. After the fix both
fetchers use the same contract — naive vendor bars are ET. This test verifies
the localisation logic produces the expected UTC offset.
"""

import os
import sys
import unittest
from datetime import datetime

import pandas as pd

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _localize_like_fetch_vix(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Mirror the post-fix logic from data/fetch_vix.py lines 65-72.

    Kept as a separate helper so a test can verify the contract without
    running yfinance.
    """
    if idx.tz is None:
        return idx.tz_localize("America/New_York").tz_convert("UTC")
    return idx.tz_convert("UTC")


class TestVixTimezoneLocalisation(unittest.TestCase):

    def test_naive_index_treated_as_eastern_in_winter(self):
        # January 2024 → EST = UTC-5. Midnight ET → 05:00 UTC.
        naive = pd.DatetimeIndex(["2024-01-15 00:00:00"])
        result = _localize_like_fetch_vix(naive)

        self.assertEqual(str(result.tz), "UTC")
        # Midnight ET in January = 05:00 UTC the same calendar day.
        self.assertEqual(result[0], pd.Timestamp("2024-01-15 05:00:00", tz="UTC"))

    def test_naive_index_treated_as_eastern_in_summer(self):
        # July 2024 → EDT = UTC-4. Midnight ET → 04:00 UTC.
        naive = pd.DatetimeIndex(["2024-07-15 00:00:00"])
        result = _localize_like_fetch_vix(naive)

        self.assertEqual(str(result.tz), "UTC")
        self.assertEqual(result[0], pd.Timestamp("2024-07-15 04:00:00", tz="UTC"))

    def test_existing_tz_is_converted_not_relocalised(self):
        # Already-tz-aware index should be converted, never localised again.
        aware = pd.DatetimeIndex(["2024-01-15 12:00:00"], tz="America/New_York")
        result = _localize_like_fetch_vix(aware)

        self.assertEqual(str(result.tz), "UTC")
        # 12:00 ET in January = 17:00 UTC
        self.assertEqual(result[0], pd.Timestamp("2024-01-15 17:00:00", tz="UTC"))

    def test_naive_not_treated_as_utc(self):
        """Regression: the OLD bug treated naive as UTC; this test enforces the fix."""
        naive = pd.DatetimeIndex(["2024-01-15 00:00:00"])
        result = _localize_like_fetch_vix(naive)

        # Under the OLD bug, result[0] would equal 2024-01-15 00:00 UTC.
        # Under the fix it must be 04:00 or 05:00 UTC depending on DST.
        # In January (EST) it must be 05:00 UTC — not 00:00.
        self.assertNotEqual(result[0], pd.Timestamp("2024-01-15 00:00:00", tz="UTC"))


class TestFetchVixSourceCodeUsesEastern(unittest.TestCase):
    """Belt-and-braces: assert the source file actually uses ``America/New_York``.

    This catches a regression where the fix is reverted by accident.
    """

    def test_source_uses_new_york_not_utc(self):
        path = os.path.join(_BACKEND_DIR, "data", "fetch_vix.py")
        with open(path, "r") as f:
            source = f.read()

        self.assertIn(
            'tz_localize("America/New_York")',
            source,
            "data/fetch_vix.py must tz_localize naive vendor timestamps as "
            "America/New_York (matches data/fetcher.py contract).",
        )
        # Make sure the OLD pattern is gone.
        # We allow tz_localize("UTC") only after tz_localize("America/New_York")
        # in the same expression — check that the standalone UTC localise is absent.
        self.assertNotIn(
            'df.index = df.index.tz_localize("UTC")',
            source,
            "data/fetch_vix.py should not tz_localize naive timestamps directly "
            "as UTC; treat them as America/New_York first.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
