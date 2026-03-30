"""
backtesting/config.py — Walk-forward dates and backtesting constants.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)

# ---------------------------------------------------------------------------
# Walk-forward period boundaries
# ---------------------------------------------------------------------------
# 7 years in-sample: 2 bull markets, 1 major crash (2020), 1 bear market (2022)
# ~3 years out-of-sample: the honest performance score
IN_SAMPLE_START = "2016-03-28"
IN_SAMPLE_END = "2022-12-31"
OUT_SAMPLE_START = "2023-01-01"
OUT_SAMPLE_END = datetime.now(timezone.utc).date().isoformat()
FULL_START = "2016-03-28"

# ---------------------------------------------------------------------------
# Risk / position-sizing params (same as Phase 3 .env)
# ---------------------------------------------------------------------------
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", 100_000.0))
MAX_PORTFOLIO_RISK = float(os.environ.get("MAX_PORTFOLIO_RISK", 0.02))
ATR_MULTIPLIER = float(os.environ.get("ATR_MULTIPLIER", 2.0))
TAKE_PROFIT_ATR_MULT = float(os.environ.get("TAKE_PROFIT_ATR_MULT", 3.0))
MAX_POSITION_SIZE = float(os.environ.get("MAX_POSITION_SIZE", 0.10))
MAX_TOTAL_EXPOSURE = float(os.environ.get("MAX_TOTAL_EXPOSURE", 0.80))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", 20))
MAX_SECTOR_EXPOSURE = float(os.environ.get("MAX_SECTOR_EXPOSURE", 0.30))
MIN_SIGNAL_STRENGTH = float(os.environ.get("MIN_SIGNAL_STRENGTH", 0.10))

# Commission rate for backtesting (0.1%)
COMMISSION = 0.001


def get_period_dates(period: str) -> tuple[str, str]:
    """Return (start_date, end_date) strings for the given period name."""
    if period == "in_sample":
        return IN_SAMPLE_START, IN_SAMPLE_END
    elif period == "out_of_sample":
        return OUT_SAMPLE_START, OUT_SAMPLE_END
    elif period == "full":
        return FULL_START, OUT_SAMPLE_END
    else:
        raise ValueError(f"Unknown period: {period!r}. Use 'in_sample', 'out_of_sample', or 'full'.")
