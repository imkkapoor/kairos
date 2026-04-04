"""
scheduler.py — Long-running weekday job scheduler for Kairos.

Jobs (all times Eastern Time, DST-aware via zoneinfo):
  07:00 ET weekdays  — Morning digest placeholder (Phase 6)
  17:00 ET weekdays  — Incremental OHLCV update via update_all()
  17:15 ET weekdays  — Strategy scan: compute indicators + generate signals
  09:31 ET weekdays  — Morning execution: live batch price fetch + execute signals
  Hourly (10:00–15:58 ET weekdays) — Intraday management: live price check,
                                      stop/TP/trailing-stop evaluation
  Every hour (daily) — DB health check (runs weekends too)

The scheduler polls every 30 seconds. ET-timed jobs use a zoneinfo-based
window check so they fire at the correct wall-clock time regardless of the
host machine's local timezone or DST transitions.
"""

import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import schedule
from loguru import logger

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.connection import ping    # noqa: E402
from data.fetcher import update_all  # noqa: E402

_ET = ZoneInfo("America/New_York")

# Track which (date, hour, minute) slots have already fired to prevent
# double-firing if the poll loop straddles a minute boundary.
_fired: set[tuple] = set()


def _is_weekday() -> bool:
    """Return True if today is Monday–Friday in ET."""
    return datetime.now(_ET).weekday() < 5


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


def _nyse_holidays(year: int) -> set[date]:
    """Return the set of NYSE market holidays for *year*.

    NYSE observes: New Year's Day, MLK Day, Presidents' Day, Good Friday,
    Memorial Day, Juneteenth, Independence Day, Labor Day,
    Thanksgiving, Christmas.  Weekend observances are shifted to the
    nearest weekday (Fri if Sat, Mon if Sun).
    """
    def _observe(d: date) -> date:
        if d.weekday() == 5:   # Saturday → Friday
            return d - timedelta(days=1)
        if d.weekday() == 6:   # Sunday → Monday
            return d + timedelta(days=1)
        return d

    def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """Return the *n*-th occurrence (1-based) of *weekday* in month/year."""
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return first + timedelta(days=delta + 7 * (n - 1))

    def _last_weekday(year: int, month: int, weekday: int) -> date:
        """Return the last occurrence of *weekday* in month/year."""
        # Start from the last day of the month and walk back
        if month == 12:
            last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(year, month + 1, 1) - timedelta(days=1)
        delta = (last.weekday() - weekday) % 7
        return last - timedelta(days=delta)

    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)

    holidays = {
        _observe(date(year, 1, 1)),                        # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),                       # Presidents' Day (3rd Mon Feb)
        good_friday,                                        # Good Friday
        _last_weekday(year, 5, 0),                         # Memorial Day (last Mon May)
        _observe(date(year, 6, 19)),                        # Juneteenth
        _observe(date(year, 7, 4)),                         # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving (4th Thu Nov)
        _observe(date(year, 12, 25)),                       # Christmas
    }
    return holidays


def _tsx_holidays(year: int) -> set[date]:
    """Return the set of TSX market holidays for *year*.

    TSX observes: New Year's Day, Family Day (3rd Mon Feb, Ontario),
    Good Friday, Victoria Day (Mon before May 25), Canada Day,
    Civic Holiday (1st Mon Aug), Labour Day (1st Mon Sep),
    Thanksgiving (2nd Mon Oct), Christmas, Boxing Day.
    Weekend observances shifted to nearest weekday.
    """
    def _observe(d: date) -> date:
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return first + timedelta(days=delta + 7 * (n - 1))

    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)

    # Victoria Day: Monday strictly preceding May 25.
    # If May 25 is itself a Monday, go back a full week to May 18.
    may25 = date(year, 5, 25)
    days_back = may25.weekday() or 7   # weekday()==0 (Mon) → 7; else → weekday()
    victoria_day = may25 - timedelta(days=days_back)

    holidays = {
        _observe(date(year, 1, 1)),                        # New Year's Day
        _nth_weekday(year, 2, 0, 3),                       # Family Day (3rd Mon Feb)
        good_friday,                                        # Good Friday
        victoria_day,                                       # Victoria Day
        _observe(date(year, 7, 1)),                         # Canada Day
        _nth_weekday(year, 8, 0, 1),                       # Civic Holiday (1st Mon Aug)
        _nth_weekday(year, 9, 0, 1),                       # Labour Day (1st Mon Sep)
        _nth_weekday(year, 10, 0, 2),                      # Thanksgiving (2nd Mon Oct)
        _observe(date(year, 12, 25)),                       # Christmas
        _observe(date(year, 12, 26)),                       # Boxing Day
    }
    return holidays


def _is_trading_day() -> bool:
    """Return True only if today is a NYSE trading day (weekday, not a holiday)."""
    today = datetime.now(_ET).date()
    if today.weekday() >= 5:   # weekend
        return False
    return today not in _nyse_holidays(today.year)


def _is_any_market_open() -> bool:
    """Return True if NYSE or TSX is open today (used for position management).

    On NYSE-only holidays (MLK Day, Presidents' Day, Memorial Day, Juneteenth,
    US Thanksgiving) the TSX is still open, so CA positions need managing.
    """
    today = datetime.now(_ET).date()
    if today.weekday() >= 5:
        return False
    year = today.year
    nyse_open = today not in _nyse_holidays(year)
    tsx_open  = today not in _tsx_holidays(year)
    return nyse_open or tsx_open


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

def _job_update_all() -> None:
    """17:00 ET weekdays — fetch latest OHLCV bars for all watchlist tickers."""
    if not _is_trading_day():
        logger.info("Scheduler: skipping update_all — not a NYSE trading day")
        return
    logger.info("Scheduler: starting update_all()")
    rows = update_all()
    logger.info(f"Scheduler: update_all() finished — {rows} rows inserted")


def _job_strategy_scan() -> None:
    """17:15 ET weekdays — run daily signal scan."""
    if not _is_trading_day():
        logger.info("Scheduler: skipping strategy scan — not a NYSE trading day")
        return
    from strategies.scanner import run_daily_scan
    run_daily_scan()


def _job_morning_execution() -> None:
    """09:31 ET weekdays — fetch live prices in batch + execute last night's signals."""
    if not _is_trading_day():
        logger.info("Scheduler: skipping morning execution — not a NYSE trading day")
        return
    from simulator.simulator import run_morning
    run_morning()


def _job_intraday_management() -> None:
    """10:00–15:58 ET weekdays (hourly) — live price check + position management."""
    if not _is_any_market_open():
        logger.info("Scheduler: skipping intraday management — all markets closed")
        return
    from simulator.simulator import run_intraday
    run_intraday()


def _job_morning_digest() -> None:
    """07:00 ET weekdays — morning digest (Phase 6)."""
    logger.info("Morning digest — Phase 6")


def _job_health_check() -> None:
    """Hourly — DB health check (runs every day including weekends)."""
    if ping():
        logger.debug("Health check: DB reachable")
    else:
        logger.error("Health check: DB UNREACHABLE — is kairos_db running? Try 'make up'")


# ---------------------------------------------------------------------------
# ET-aware time dispatcher
# ---------------------------------------------------------------------------

# (hour_ET, minute_ET, weekday_only, job_function)
_ET_JOBS = [
    (7,  0,  True,  _job_morning_digest),
    (9,  31, True,  _job_morning_execution),
    (10, 0,  True,  _job_intraday_management),
    (11, 0,  True,  _job_intraday_management),
    (12, 0,  True,  _job_intraday_management),
    (13, 0,  True,  _job_intraday_management),
    (14, 0,  True,  _job_intraday_management),
    (15, 0,  True,  _job_intraday_management),
    (15, 58, True,  _job_intraday_management),
    (17, 0,  True,  _job_update_all),
    (17, 15, True,  _job_strategy_scan),
]


def _dispatch_et_jobs() -> None:
    """Called every minute: fire any ET-timed jobs that are due now."""
    now_et  = datetime.now(_ET)
    is_weekday = now_et.weekday() < 5
    slot_date  = now_et.date()

    # Prune old fire history (keep only today's entries)
    stale = {k for k in _fired if k[0] != slot_date}
    _fired.difference_update(stale)

    for hour, minute, weekday_only, job_fn in _ET_JOBS:
        if weekday_only and not is_weekday:
            continue
        if now_et.hour != hour or now_et.minute != minute:
            continue
        key = (slot_date, hour, minute)
        if key in _fired:
            continue
        _fired.add(key)
        try:
            job_fn()
        except Exception as exc:
            logger.exception(f"Job {job_fn.__name__} raised an exception: {exc}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Kairos scheduler starting...")

    if not ping():
        logger.error(
            "Cannot reach the database. Is kairos_db running? Run 'make up' first."
        )
        sys.exit(1)

    logger.info("DB connection OK")

    # Minute-level dispatcher for ET-timed jobs
    schedule.every(1).minutes.do(_dispatch_et_jobs)

    # Hourly health check (system-clock based — timezone doesn't matter for this)
    schedule.every(1).hours.do(_job_health_check)

    logger.info(
        "Scheduler running. "
        "ET jobs: 07:00 digest (P6), 09:31 morning execution, "
        "10:00-15:58 intraday management (hourly), "
        "17:00 fetch, 17:15 scan. "
        "Hourly health check active. "
        "Press Ctrl+C to stop."
    )

    
    print(r"""
.------..------..------..------..------..------.
|K.--. ||A.--. ||I.--. ||R.--. ||O.--. ||S.--. |
| :/\: || (\/) || (\/) || :(): || :/\: || :/\: |
| :\/: || :\/: || :\/: || ()() || :\/: || :\/: |
| '--'K|| '--'A|| '--'I|| '--'R|| '--'O|| '--'S|
`------'`------'`------'`------'`------'`------'
    """)

    try:
        while True:
            schedule.run_pending()
            _time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
