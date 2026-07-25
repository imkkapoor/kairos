"""
scheduler.py — Long-running weekday job scheduler for Kairos.

Jobs (all times Eastern Time, DST-aware via zoneinfo):
  07:00 ET weekdays  — Morning digest placeholder (Phase 6)
  17:00 ET weekdays  — Incremental OHLCV update via update_all()
  17:15 ET weekdays  — Strategy scan: compute indicators + generate signals
  17:25 ET weekdays  — Evening management: stop/TP/exit checks on DB close prices
  09:31 ET weekdays  — Morning execution: live batch price fetch + execute signals
  Every 15 min (10:00–15:58 ET weekdays) — Intraday management: live price check,
                                            stop/TP/trailing-stop evaluation
  Every hour (daily) — DB health check (runs weekends too)

The scheduler polls every 30 seconds. ET-timed jobs use a zoneinfo-based
window check so they fire at the correct wall-clock time regardless of the
host machine's local timezone or DST transitions.
"""

import sys
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule
from loguru import logger

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.connection import ping, get_latest_timestamp  # noqa: E402
from data.fetcher import update_all  # noqa: E402
from utils.trading_calendar import (  # noqa: E402
    is_nyse_trading_day,
    is_any_market_open,
)

_ET = ZoneInfo("America/New_York")

# Track which (date, hour, minute) slots have already fired to prevent
# double-firing if the poll loop straddles a minute boundary.
_fired: set[tuple] = set()


def _is_weekday() -> bool:
    """Return True if today is Monday–Friday in ET."""
    return datetime.now(_ET).weekday() < 5


def _is_trading_day() -> bool:
    """Return True only if today is a NYSE trading day (weekday, not a holiday)."""
    return is_nyse_trading_day(datetime.now(_ET).date())


def _is_any_market_open() -> bool:
    """Return True if NYSE or TSX is open today (used for position management).

    On NYSE-only holidays (MLK Day, Presidents' Day, Memorial Day, Juneteenth,
    US Thanksgiving) the TSX is still open, so CA positions need managing.
    """
    return is_any_market_open(datetime.now(_ET).date())


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

    # SPY-late check: confirm today's bar landed. If yfinance ever shifts later
    # than 17:00 ET, this warning is how we'll know to push the schedule again.
    today_et = datetime.now(_ET).date()
    spy_latest = get_latest_timestamp("SPY", "1d")
    if spy_latest is None or spy_latest.astimezone(_ET).date() < today_et:
        logger.warning(
            f"Scheduler: SPY 1d bar for {today_et} not in DB after update_all "
            f"(latest={spy_latest}). yfinance may be late; today's scan will use "
            f"stale indicators."
        )


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
    """10:00–15:58 ET weekdays (every 15 min) — live price check + position management."""
    if not _is_any_market_open():
        logger.info("Scheduler: skipping intraday management — all markets closed")
        return
    from simulator.simulator import run_intraday
    run_intraday()


def _job_evening_management() -> None:
    """17:25 ET weekdays — position management on today's DB close prices."""
    if not _is_trading_day():
        logger.info("Scheduler: skipping evening management — not a NYSE trading day")
        return
    from simulator.simulator import run_evening
    run_evening()


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
    (17, 0,  True,  _job_update_all),
    (17, 15, True,  _job_strategy_scan),
    (17, 25, True,  _job_evening_management),
]

# Intraday position management every 15 minutes while the market is open
# (10:00–15:45 ET), plus a final near-close check at 15:58.
for _h in range(10, 16):
    for _m in (0, 15, 30, 45):
        _ET_JOBS.append((_h, _m, True, _job_intraday_management))
_ET_JOBS.append((15, 58, True, _job_intraday_management))


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
        "10:00-15:58 intraday management (every 15 min), "
        "17:00 fetch, 17:15 scan, 17:25 evening management. "
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
