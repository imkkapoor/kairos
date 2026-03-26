"""
strategies/scanner.py — Daily signal scan orchestrator.

Steps:
  1. compute_all()                 → today_indicators
  2. get_price_data("SPY", limit=10) → spy_df  (for crisis detection)
  3. detect_all(today_indicators, spy_df) → regimes
  4. get_open_position_tickers()   → open_tickers
  5. tickers = list(today_indicators.keys())
  6. get_prev_indicators(tickers)  → prev_indicators
  7. rsi.generate_signals(...)     → rsi_signals
  8. momentum.generate_signals(…)  → mom_signals
  9. insert_signals(all_signals)   → signal_ids
 10. Print daily summary
"""

from datetime import date, timezone, datetime

from loguru import logger

from db.connection import (
    get_price_data,
    get_open_position_tickers,
    get_prev_indicators,
    insert_signals,
)
from strategies.indicators import compute_all
from strategies.regime import detect_all, detect_crisis
import strategies.rsi as rsi_strategy
import strategies.momentum as momentum_strategy


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def _avg_strength(sigs: list[dict]) -> float:
    if not sigs:
        return 0.0
    return sum(s["strength"] for s in sigs) / len(sigs)


def _count_type(sigs: list[dict], sig_type: str) -> int:
    return sum(1 for s in sigs if s["signal_type"] == sig_type)


def _skipped_open(tickers: list[str], open_set: set[str]) -> int:
    """Count tickers in today_indicators that are open positions (would have been guards)."""
    return sum(1 for t in tickers if t in open_set)


def _print_summary(
    today_str: str,
    total_tickers: int,
    computed: int,
    regimes: dict[str, dict],
    rsi_signals: list[dict],
    mom_signals: list[dict],
    skipped_open_count: int,
    all_signals: list[dict],
) -> None:
    trending = sum(1 for r in regimes.values() if r["regime"] == "TRENDING")
    choppy   = sum(1 for r in regimes.values() if r["regime"] == "CHOPPY")
    crisis   = sum(1 for r in regimes.values() if r["regime"] == "CRISIS")
    skipped  = total_tickers - computed

    rsi_buy  = _count_type(rsi_signals, "BUY")
    rsi_sell = _count_type(rsi_signals, "SELL")
    mom_buy  = _count_type(mom_signals, "BUY")
    mom_sell = _count_type(mom_signals, "SELL")
    rsi_avg  = _avg_strength(rsi_signals)
    mom_avg  = _avg_strength(mom_signals)

    top5 = sorted(all_signals, key=lambda s: s["strength"], reverse=True)[:5]

    print(f"-- Kairos signal scan — {today_str} --")
    print(f"Tickers scanned: {total_tickers} | Computed: {computed} | Skipped: {skipped}")
    print(f"Regime: Trending {trending} | Choppy {choppy} | Crisis {crisis}")
    print(f"RSI:      {rsi_buy} buy | {rsi_sell} sell (avg strength {rsi_avg:.2f})")
    print(f"Momentum: {mom_buy} buy | {mom_sell} sell (avg strength {mom_avg:.2f})")
    print(f"Skipped (open position): {skipped_open_count}")
    if top5:
        print("Top 5 by strength:")
        for s in top5:
            print(f"  {s['ticker']:8s} {s['signal_type']:4s} {s['strategy']:10s} {s['strength']:.4f}  \"{s['reason']}\"")
    print("-------------------------------------------")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_daily_scan(interval: str = "1d") -> None:
    """Run the full Kairos daily signal scan and persist results to the DB."""
    today_str = date.today().isoformat()
    logger.info(f"[scanner] Starting daily scan for {today_str}")

    # 1. Compute (or retrieve from DB) all indicator rows
    today_indicators = compute_all(interval=interval)
    total_tickers    = len(today_indicators) if today_indicators else 0
    computed         = total_tickers
    logger.info(f"[scanner] Indicators ready for {computed} tickers")

    if not today_indicators:
        logger.warning("[scanner] No indicator rows — aborting scan")
        return

    # 2. SPY price data for crisis detection
    try:
        spy_df = get_price_data("SPY", interval=interval, limit=10)
    except Exception as exc:
        logger.warning(f"[scanner] Could not fetch SPY data: {exc} — defaulting to non-crisis")
        spy_df = None

    # 3. Regime detection
    regimes = detect_all(today_indicators, spy_df)
    logger.info(
        f"[scanner] Regimes: "
        f"TRENDING={sum(1 for r in regimes.values() if r['regime']=='TRENDING')} "
        f"CHOPPY={sum(1 for r in regimes.values() if r['regime']=='CHOPPY')} "
        f"CRISIS={sum(1 for r in regimes.values() if r['regime']=='CRISIS')}"
    )

    # 4. Open positions (duplicate guard)
    try:
        open_tickers = get_open_position_tickers()
    except Exception as exc:
        logger.warning(f"[scanner] Could not fetch open positions: {exc} — assuming none")
        open_tickers = []
    open_set = set(open_tickers)

    # 5. Ticker list
    tickers = list(today_indicators.keys())

    # 6. Previous indicator rows (for crossover detection)
    try:
        prev_indicators = get_prev_indicators(tickers, interval=interval)
    except Exception as exc:
        logger.warning(f"[scanner] Could not fetch prev indicators: {exc} — crossovers disabled")
        prev_indicators = {}

    # 7. RSI signals
    rsi_signals = rsi_strategy.generate_signals(
        tickers, today_indicators, regimes, open_tickers
    )
    logger.info(f"[scanner] RSI signals: {len(rsi_signals)}")

    # 8. Momentum signals
    mom_signals = momentum_strategy.generate_signals(
        tickers, today_indicators, prev_indicators, regimes, open_tickers
    )
    logger.info(f"[scanner] Momentum signals: {len(mom_signals)}")

    # 9. Persist all signals
    all_signals = rsi_signals + mom_signals
    if all_signals:
        try:
            signal_ids = insert_signals(all_signals)
            logger.info(f"[scanner] Inserted {len(signal_ids)} signals: {signal_ids}")
        except Exception as exc:
            logger.error(f"[scanner] Failed to insert signals: {exc}")
    else:
        logger.info("[scanner] No signals generated today")

    # Count open-position skips (informational)
    skipped_open_count = _skipped_open(tickers, open_set)

    # 10. Print summary
    _print_summary(
        today_str=today_str,
        total_tickers=total_tickers,
        computed=computed,
        regimes=regimes,
        rsi_signals=rsi_signals,
        mom_signals=mom_signals,
        skipped_open_count=skipped_open_count,
        all_signals=all_signals,
    )

    logger.info("[scanner] Daily scan complete")
