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
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

def _job_update_all() -> None:
    """17:00 ET weekdays — fetch latest OHLCV bars for all watchlist tickers."""
    logger.info("Scheduler: starting update_all()")
    rows = update_all()
    logger.info(f"Scheduler: update_all() finished — {rows} rows inserted")


def _job_strategy_scan() -> None:
    """17:15 ET weekdays — run daily signal scan."""
    from strategies.scanner import run_daily_scan
    run_daily_scan()


def _job_morning_execution() -> None:
    """09:31 ET weekdays — fetch live prices in batch + execute last night's signals."""
    if not _is_weekday():
        return
    from simulator.simulator import run_morning
    run_morning()


def _job_intraday_management() -> None:
    """10:00–15:58 ET weekdays (hourly) — live price check + position management."""
    if not _is_weekday():
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
