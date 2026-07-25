"""
tests/test_trading_calendar.py — Holiday-aware trading calendar.

Run from backend/:
    python -m unittest tests.test_trading_calendar -v
"""

import os
import sys
import unittest
from datetime import date

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from utils.trading_calendar import (
    is_nyse_trading_day,
    is_tsx_trading_day,
    is_any_market_open,
    nyse_holidays,
    tsx_holidays,
    prev_trading_day,
    _easter_sunday,
)


class TestHolidaySets(unittest.TestCase):
    """Verify the known-date holiday calendars produce the right dates."""

    def test_easter_sunday(self):
        # Known Easter Sundays (Gregorian)
        self.assertEqual(_easter_sunday(2024), date(2024, 3, 31))
        self.assertEqual(_easter_sunday(2025), date(2025, 4, 20))
        self.assertEqual(_easter_sunday(2026), date(2026, 4, 5))

    def test_nyse_holidays_2026(self):
        h = nyse_holidays(2026)
        # New Year's Day
        self.assertIn(date(2026, 1, 1), h)
        # MLK Day — 3rd Monday of January 2026 = Jan 19
        self.assertIn(date(2026, 1, 19), h)
        # Presidents' Day — 3rd Monday of February 2026 = Feb 16
        self.assertIn(date(2026, 2, 16), h)
        # Good Friday 2026 = April 3
        self.assertIn(date(2026, 4, 3), h)
        # Memorial Day 2026 = last Monday of May = May 25
        self.assertIn(date(2026, 5, 25), h)
        # Juneteenth = June 19 (Friday in 2026)
        self.assertIn(date(2026, 6, 19), h)
        # Independence Day 2026 = July 4 is Saturday → observed Friday July 3
        self.assertIn(date(2026, 7, 3), h)
        # Labor Day 2026 = first Monday of September = Sep 7
        self.assertIn(date(2026, 9, 7), h)
        # Thanksgiving 2026 = 4th Thursday of November = Nov 26
        self.assertIn(date(2026, 11, 26), h)
        # Christmas 2026 = Friday Dec 25
        self.assertIn(date(2026, 12, 25), h)

    def test_nyse_does_not_include_random_dates(self):
        h = nyse_holidays(2026)
        # Tuesday in April 2026 — not a holiday
        self.assertNotIn(date(2026, 4, 14), h)
        # Christmas Eve — not an NYSE holiday (early close, but not closed)
        self.assertNotIn(date(2026, 12, 24), h)

    def test_tsx_holidays_2026(self):
        h = tsx_holidays(2026)
        # Family Day — 3rd Monday February 2026 = Feb 16
        self.assertIn(date(2026, 2, 16), h)
        # Good Friday 2026
        self.assertIn(date(2026, 4, 3), h)
        # Canada Day = July 1 2026 (Wed)
        self.assertIn(date(2026, 7, 1), h)
        # Civic Holiday — 1st Monday August 2026 = Aug 3
        self.assertIn(date(2026, 8, 3), h)
        # Labour Day — 1st Monday September 2026 = Sep 7
        self.assertIn(date(2026, 9, 7), h)
        # Canadian Thanksgiving — 2nd Monday October 2026 = Oct 12
        self.assertIn(date(2026, 10, 12), h)
        # Boxing Day 2026 = Dec 26 is Saturday → observed Friday Dec 25 (also Christmas)
        # The observe shift means Boxing Day Sat → Fri (Dec 25 = Christmas).
        # That collision means our frozenset only sees one entry, which is fine.

    def test_victoria_day_when_may25_is_monday(self):
        # 2026: May 25 is Monday → Victoria Day should be May 18 (go back a full week)
        h = tsx_holidays(2026)
        self.assertIn(date(2026, 5, 18), h)
        self.assertNotIn(date(2026, 5, 25), h)


class TestTradingDayPredicates(unittest.TestCase):

    def test_weekday_non_holiday(self):
        self.assertTrue(is_nyse_trading_day(date(2026, 6, 10)))  # Wednesday
        self.assertTrue(is_tsx_trading_day(date(2026, 6, 10)))

    def test_weekend_is_not_trading(self):
        self.assertFalse(is_nyse_trading_day(date(2026, 6, 6)))  # Saturday
        self.assertFalse(is_nyse_trading_day(date(2026, 6, 7)))  # Sunday
        self.assertFalse(is_tsx_trading_day(date(2026, 6, 6)))

    def test_holiday_is_not_trading(self):
        self.assertFalse(is_nyse_trading_day(date(2026, 4, 3)))   # Good Friday
        self.assertFalse(is_nyse_trading_day(date(2026, 11, 26))) # Thanksgiving

    def test_any_market_open(self):
        # MLK Day 2026 (Jan 19) — NYSE closed, TSX open
        self.assertTrue(is_any_market_open(date(2026, 1, 19)))
        # Good Friday 2026 — both closed
        self.assertFalse(is_any_market_open(date(2026, 4, 3)))
        # Weekend — both closed
        self.assertFalse(is_any_market_open(date(2026, 6, 7)))


class TestPrevTradingDay(unittest.TestCase):
    """The bug-fix this whole ticket targets — holiday-aware lookback."""

    def test_simple_weekday(self):
        # Wednesday → Tuesday
        self.assertEqual(prev_trading_day(date(2026, 6, 10)), date(2026, 6, 9))

    def test_monday_goes_back_to_friday(self):
        # Monday June 8 2026 → Friday June 5 (no holiday in between)
        self.assertEqual(prev_trading_day(date(2026, 6, 8)), date(2026, 6, 5))

    def test_sunday_goes_back_to_friday(self):
        # Sunday June 7 → Friday June 5
        self.assertEqual(prev_trading_day(date(2026, 6, 7)), date(2026, 6, 5))

    def test_saturday_goes_back_to_friday(self):
        self.assertEqual(prev_trading_day(date(2026, 6, 6)), date(2026, 6, 5))

    def test_monday_after_good_friday(self):
        """The original L6 bug: Mon after Good Friday must return Thursday, not Friday."""
        # Good Friday 2026 = April 3 (Fri). Monday = April 6.
        # The OLD weekday-only walk returned April 3 (a closed market day).
        # The NEW walk must skip Good Friday → April 2 (Thursday).
        self.assertEqual(prev_trading_day(date(2026, 4, 6)), date(2026, 4, 2))

    def test_tuesday_after_memorial_day(self):
        # Memorial Day 2026 = May 25 (Mon). Tuesday = May 26.
        # Must skip Memorial Day → previous trading day is Friday May 22.
        self.assertEqual(prev_trading_day(date(2026, 5, 26)), date(2026, 5, 22))

    def test_tuesday_after_mlk(self):
        # MLK Day 2026 = Jan 19 (Mon). Tuesday = Jan 20.
        # Must skip MLK Day → previous trading day is Friday Jan 16.
        self.assertEqual(prev_trading_day(date(2026, 1, 20)), date(2026, 1, 16))

    def test_day_after_thanksgiving(self):
        # Thanksgiving 2026 = Nov 26 (Thu). Black Friday Nov 27 is a half-day
        # but NYSE is open. prev_trading_day(Nov 27) should be Nov 25 (Wed).
        self.assertEqual(prev_trading_day(date(2026, 11, 27)), date(2026, 11, 25))

    def test_monday_after_christmas_weekend(self):
        # Christmas 2026 = Friday Dec 25. Sat 26, Sun 27, Mon 28.
        # prev_trading_day(Dec 28) must skip Christmas → Thursday Dec 24.
        # (Christmas Eve Dec 24 is a half-day but open.)
        self.assertEqual(prev_trading_day(date(2026, 12, 28)), date(2026, 12, 24))

    def test_back_to_back_holiday_chain(self):
        # New Year's Day 2027 = Friday Jan 1 (observed).
        # prev_trading_day(Mon Jan 4 2027) must skip Fri Jan 1 → Thu Dec 31 2026.
        self.assertEqual(prev_trading_day(date(2027, 1, 4)), date(2026, 12, 31))

    def test_strictly_less_than_anchor(self):
        # Result must always be strictly < input.
        for d in [date(2026, 6, 10), date(2026, 6, 8), date(2026, 4, 6)]:
            self.assertLess(prev_trading_day(d), d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
