#!/bin/bash
# Replay missed trading days: May 9 – June 5, 2026
# Order per day: simulate (morning) → simulate-evening → scan (next day's signals)
# Skips: weekends, Memorial Day (May 26)
# Note: May 18 = Victoria Day (TSX closed, NYSE open — included)

set -e

echo "========================================================"
echo "Step 1/2: Fetching missing OHLCV prices (May 7 → today)"
echo "========================================================"
make fetch

echo ""
echo "========================================================"
echo "Step 2/2: Backfilling missing USDCAD FX rates"
echo "========================================================"
make hydrate-fx

echo ""
echo "========================================================"
echo "Starting day-by-day replay..."
echo "========================================================"

TRADING_DAYS=(
    "2026-05-09"
    "2026-05-12"
    "2026-05-13"
    "2026-05-14"
    "2026-05-15"
    "2026-05-16"
    "2026-05-19"  # Victoria Day (TSX closed, NYSE open)
    "2026-05-20"
    "2026-05-21"
    "2026-05-22"
    "2026-05-23"
    # 2026-05-26 = Memorial Day — skipped
    "2026-05-27"
    "2026-05-28"
    "2026-05-29"
    "2026-05-30"
    "2026-06-01"
    "2026-06-02"
    "2026-06-03"
    "2026-06-04"
    "2026-06-05"
)

TOTAL=${#TRADING_DAYS[@]}
COUNT=0

for DATE in "${TRADING_DAYS[@]}"; do
    COUNT=$((COUNT + 1))
    echo ""
    echo "========================================================"
    echo "[$COUNT/$TOTAL] $DATE"
    echo "========================================================"

    echo ">>> Morning simulate"
    make simulate DATE=$DATE

    echo ">>> Evening simulate"
    make simulate-evening DATE=$DATE

    echo ">>> Daily scan"
    make scan DATE=$DATE

    echo "<<< Done: $DATE"
done

echo ""
echo "========================================================"
echo "Catch-up complete through 2026-06-05 ($TOTAL days)"
echo "========================================================"
