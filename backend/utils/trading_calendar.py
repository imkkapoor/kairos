"""
utils/trading_calendar.py — NYSE / TSX holiday calendars and trading-day helpers.

Single source of truth for "is this date a trading day?" across the codebase.
Both scheduler.py (live dispatch) and simulator.py (replay / signal lookup) read
from here so they cannot drift.

Calendars
---------
NYSE observes: New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial
Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.

TSX observes: New Year's Day, Family Day (3rd Mon Feb, Ontario), Good Friday,
Victoria Day (Mon before May 25), Canada Day, Civic Holiday (1st Mon Aug),
Labour Day (1st Mon Sep), Thanksgiving (2nd Mon Oct), Christmas, Boxing Day.

Weekend observances are shifted to the nearest weekday (Fri if Sat, Mon if Sun).
"""

from datetime import date, timedelta
from functools import lru_cache


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def _easter_sunday(year: int) -> date:
    """Return the date of Easter Sunday for *year* (Gregorian calendar)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    month = (h + lo - 7 * m + 114) // 31
    day = ((h + lo - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observe(d: date) -> date:
    """Shift weekend observances to the nearest weekday: Sat→Fri, Sun→Mon."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the *n*-th occurrence (1-based) of *weekday* in month/year."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of *weekday* in month/year."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


# ---------------------------------------------------------------------------
# Holiday sets (memoised — these never change)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> frozenset[date]:
    """Return the set of NYSE market holidays for *year*."""
    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)

    return frozenset({
        _observe(date(year, 1, 1)),         # New Year's Day
        _nth_weekday(year, 1, 0, 3),        # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),        # Presidents' Day (3rd Mon Feb)
        good_friday,                         # Good Friday
        _last_weekday(year, 5, 0),          # Memorial Day (last Mon May)
        _observe(date(year, 6, 19)),         # Juneteenth
        _observe(date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),        # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),       # Thanksgiving (4th Thu Nov)
        _observe(date(year, 12, 25)),        # Christmas
    })


@lru_cache(maxsize=64)
def tsx_holidays(year: int) -> frozenset[date]:
    """Return the set of TSX market holidays for *year*."""
    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)

    # Victoria Day: Monday strictly preceding May 25.
    # If May 25 is itself a Monday, go back a full week to May 18.
    may25 = date(year, 5, 25)
    days_back = may25.weekday() or 7   # weekday()==0 (Mon) → 7; else → weekday()
    victoria_day = may25 - timedelta(days=days_back)

    return frozenset({
        _observe(date(year, 1, 1)),         # New Year's Day
        _nth_weekday(year, 2, 0, 3),        # Family Day (3rd Mon Feb)
        good_friday,                         # Good Friday
        victoria_day,                        # Victoria Day
        _observe(date(year, 7, 1)),          # Canada Day
        _nth_weekday(year, 8, 0, 1),        # Civic Holiday (1st Mon Aug)
        _nth_weekday(year, 9, 0, 1),        # Labour Day (1st Mon Sep)
        _nth_weekday(year, 10, 0, 2),       # Thanksgiving (2nd Mon Oct)
        _observe(date(year, 12, 25)),        # Christmas
        _observe(date(year, 12, 26)),        # Boxing Day
    })


# ---------------------------------------------------------------------------
# Trading-day predicates
# ---------------------------------------------------------------------------

def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_nyse_trading_day(d: date) -> bool:
    """True when NYSE is open: not a weekend and not in the holiday set."""
    if is_weekend(d):
        return False
    return d not in nyse_holidays(d.year)


def is_tsx_trading_day(d: date) -> bool:
    """True when TSX is open: not a weekend and not in the holiday set."""
    if is_weekend(d):
        return False
    return d not in tsx_holidays(d.year)


def is_any_market_open(d: date) -> bool:
    """True if NYSE or TSX is open on *d* (used for intraday position management)."""
    return is_nyse_trading_day(d) or is_tsx_trading_day(d)


# ---------------------------------------------------------------------------
# Previous trading day walk
# ---------------------------------------------------------------------------

# Cap on the look-back walk to defend against pathological inputs (e.g. a date
# inside a region with no calendar coverage). 15 days is enough to cover the
# longest realistic weekend+holiday gap (Christmas Eve weekend run plus New
# Year's Day chain) while still bounded.
_MAX_LOOKBACK_DAYS = 15


def prev_trading_day(d: date, *, calendar: str = "nyse") -> date:
    """Return the most recent trading day strictly before *d*.

    Walks back day by day until a trading day is found. Handles weekends AND
    holidays — fixes the long-standing bug where `_last_scan_date_utc` would
    return Friday after a Good-Friday holiday and silently load no signals.

    Parameters
    ----------
    d:
        Anchor date. The result is always strictly < d.
    calendar:
        'nyse' (default) or 'tsx'.
    """
    if calendar == "nyse":
        predicate = is_nyse_trading_day
    elif calendar == "tsx":
        predicate = is_tsx_trading_day
    else:
        raise ValueError(f"prev_trading_day: unknown calendar {calendar!r}")

    candidate = d - timedelta(days=1)
    for _ in range(_MAX_LOOKBACK_DAYS):
        if predicate(candidate):
            return candidate
        candidate -= timedelta(days=1)

    raise RuntimeError(
        f"prev_trading_day: no {calendar.upper()} trading day found in "
        f"{_MAX_LOOKBACK_DAYS} days before {d}"
    )
