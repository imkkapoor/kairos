"""
strategies/scanner.py — Daily signal scan orchestrator.

Steps:
  1. compute_all()                 → today_indicators
  2. get_price_data("SPY", limit=10) → spy_df  (for crisis detection)
  3. detect_all(today_indicators, spy_df) → regimes
  4. get_open_position_tickers()   → open_tickers
  5. tickers = list(today_indicators.keys())
  6. get_prev_indicators(tickers)  → prev_indicators
  5b. Compute roc_rankings for reversal strategy
  5c. Compute sector_scores for sector rotation
  7. rsi.generate_signals(...)     → rsi_signals
  8. momentum.generate_signals(…)  → mom_signals
  8b. macd.generate_signals(…)     → macd_signals
  8c. reversal.generate_signals(…) → reversal_signals
  8d. sector_rotation.generate_signals(…) → sector_signals
  9. insert_signals(all_signals)   → signal_ids
 10. Print daily summary
"""

from datetime import date, timezone, datetime, timedelta
from typing import Optional

from loguru import logger

try:
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

from db.connection import (
    get_price_data,
    get_open_position_tickers,
    get_prev_indicators,
    get_sector_map,
    insert_signals,
)
from strategies.indicators import compute_all
from strategies.regime import detect_all, detect_crisis
import strategies.rsi as rsi_strategy
import strategies.momentum as momentum_strategy
import strategies.macd as macd_strategy
import strategies.reversal as reversal_strategy
import strategies.sector_rotation as sector_rotation_strategy


# ---------------------------------------------------------------------------
# Z-score normalisation
# ---------------------------------------------------------------------------

_Z_SCORE_CAP = 3.0  # Winsorize at ±3σ (99.7th percentile) to prevent outlier domination


def _compute_z_scores(signals: list[dict]) -> None:
    """Compute cross-universe Z-scores in place, grouped by strategy.

    For each strategy group, calculates mean and std of strength values,
    then sets z_score = clip((strength - mean) / std, -3.0, +3.0).

    Clipping at ±3σ (Winsorization) prevents a single outlier ticker from
    inflating σ and compressing all other signals toward zero. Any raw Z
    beyond ±3 is treated as equivalent to the cap — it still gets highest
    priority but doesn't receive 10x weight over a normally strong signal.

    If std is zero (all signals identical), z_score is set to 0.0.
    """
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        groups[sig.get("strategy", "unknown")].append(sig)

    for strategy, sigs in groups.items():
        strengths = [float(s.get("strength", 0.0)) for s in sigs]
        n = len(strengths)
        if n == 0:
            continue
        mean = sum(strengths) / n
        variance = sum((x - mean) ** 2 for x in strengths) / n
        std = variance ** 0.5

        if std == 0:
            for s in sigs:
                s["z_score"] = 0.0
        else:
            for s, x in zip(sigs, strengths):
                raw_z = (x - mean) / std
                s["z_score"] = max(min(raw_z, _Z_SCORE_CAP), -_Z_SCORE_CAP)

    logger.debug(
        f"[scanner] Z-scores computed for {len(signals)} signals across "
        f"{len(groups)} strategies (capped at ±{_Z_SCORE_CAP})"
    )


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
    macd_signals: list[dict],
    reversal_signals: list[dict],
    sector_signals: list[dict],
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
    macd_buy  = _count_type(macd_signals, "BUY")
    macd_sell = _count_type(macd_signals, "SELL")
    rev_buy   = _count_type(reversal_signals, "BUY")
    rev_sell  = _count_type(reversal_signals, "SELL")
    sec_buy   = _count_type(sector_signals, "BUY")
    sec_sell  = _count_type(sector_signals, "SELL")

    rsi_avg  = _avg_strength(rsi_signals)
    mom_avg  = _avg_strength(mom_signals)
    macd_avg = _avg_strength(macd_signals)
    rev_avg  = _avg_strength(reversal_signals)
    sec_avg  = _avg_strength(sector_signals)

    top5 = sorted(
        all_signals,
        key=lambda s: (s.get("z_score") if s.get("z_score") is not None else float('-inf')),
        reverse=True,
    )[:5]

    print(f"-- Kairos signal scan — {today_str} --")
    print(f"Tickers scanned: {total_tickers} | Computed: {computed} | Skipped: {skipped}")
    print(f"Regime: Trending {trending} | Choppy {choppy} | Crisis {crisis}")
    print(f"RSI:             {rsi_buy} buy | {rsi_sell} sell (avg strength {rsi_avg:.2f})")
    print(f"Momentum:        {mom_buy} buy | {mom_sell} sell (avg strength {mom_avg:.2f})")
    print(f"MACD:            {macd_buy} buy | {macd_sell} sell (avg strength {macd_avg:.2f})")
    print(f"Reversal:        {rev_buy} buy | {rev_sell} sell (avg strength {rev_avg:.2f})")
    print(f"Sector rotation: {sec_buy} buy | {sec_sell} sell (avg strength {sec_avg:.2f})")
    print(f"Skipped (open position): {skipped_open_count}")
    if top5:
        print("Top 5 by z-score:")
        for s in top5:
            z = s.get("z_score")
            z_str = f"{z:+.2f}" if z is not None else "N/A"
            print(f"  {s['ticker']:8s} {s['signal_type']:4s} {s['strategy']:16s} z={z_str} str={s['strength']:.4f}  \"{s['reason']}\"")
    print("-------------------------------------------")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_daily_scan(
    interval: str = "1d",
    for_date: Optional[datetime] = None,
) -> None:
    """Run the full Kairos daily signal scan and persist results to the DB.

    Parameters
    ----------
    for_date:
        When set, replay the scan for this historical date using price/indicator
        data already stored in the DB. Useful for catching up after missed days.
        Defaults to the most recent trading day (live mode).
    """
    if for_date is not None:
        logger.info(f"[scanner] Starting historical scan for {for_date.date()}")
    else:
        logger.info("[scanner] Starting daily scan")

    # 1. Compute (or retrieve from DB) all indicator rows
    today_indicators = compute_all(interval=interval, for_date=for_date)
    total_tickers    = len(today_indicators) if today_indicators else 0
    computed         = total_tickers
    logger.info(f"[scanner] Indicators ready for {computed} tickers")

    if not today_indicators:
        logger.warning("[scanner] No indicator rows — aborting scan")
        return

    # Derive trading day from the indicator timestamps (price bar date)
    # rather than using wall-clock UTC date, so signals and summary
    # are anchored to the correct market session.
    _sample_row = next(iter(today_indicators.values()))
    trading_day = _sample_row.get("time")
    if trading_day is not None and hasattr(trading_day, 'strftime'):
        today_str = trading_day.strftime("%Y-%m-%d")
    else:
        today_str = date.today().isoformat()
    logger.info(f"[scanner] Trading day: {today_str}")

    # 2. SPY price data for crisis detection
    try:
        spy_end = (for_date + timedelta(days=1)) if for_date is not None else None
        spy_df = get_price_data("SPY", interval=interval, limit=10, end=spy_end)
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
        prev_indicators = get_prev_indicators(tickers, interval=interval, for_date=for_date)
    except Exception as exc:
        logger.warning(f"[scanner] Could not fetch prev indicators: {exc} — crossovers disabled")
        prev_indicators = {}

    # 6b. Compute ROC rankings for reversal strategy
    all_roc = {
        ticker: row.get('roc_20')
        for ticker, row in today_indicators.items()
        if row.get('roc_20') is not None
    }
    sorted_tickers = sorted(all_roc, key=lambda t: all_roc[t])  # worst first
    roc_rankings = (
        {t: i / len(sorted_tickers) for i, t in enumerate(sorted_tickers)}
        if sorted_tickers else {}
    )

    # 6c. Compute sector scores for sector rotation
    required_etfs = ['XLE', 'XLK', 'TLT', 'XLU', 'XLV', 'SPY']
    missing_etfs = [
        e for e in required_etfs
        if e not in today_indicators or today_indicators[e].get('roc_20') is None
    ]
    if missing_etfs:
        logger.warning(f"[scanner] Sector rotation disabled — missing ETF data: {missing_etfs}")
        sector_scores = None
    else:
        xle_roc = today_indicators['XLE'].get('roc_20', 0) or 0
        xlk_roc = today_indicators['XLK'].get('roc_20', 0) or 0
        tlt_roc = today_indicators['TLT'].get('roc_20', 0) or 0
        xlu_roc = today_indicators['XLU'].get('roc_20', 0) or 0
        xlv_roc = today_indicators['XLV'].get('roc_20', 0) or 0
        spy_roc = today_indicators['SPY'].get('roc_20', 0) or 0

        xle_vs_xlk = float(xle_roc) - float(xlk_roc)
        tlt_trend  = float(tlt_roc)
        late_cycle_score = xle_vs_xlk + (-tlt_trend * 0.5)

        xlu_vs_spy = float(xlu_roc) - float(spy_roc)
        xlv_vs_spy = float(xlv_roc) - float(spy_roc)
        defensive_score = (xlu_vs_spy + xlv_vs_spy) / 2

        sector_scores = {'late_cycle': late_cycle_score, 'defensive': defensive_score}
        logger.info(f"[scanner] Sector scores: late_cycle={late_cycle_score:.2f}, defensive={defensive_score:.2f}")

    # 6d. Fetch sector map for sector rotation
    try:
        sector_map = get_sector_map()
    except Exception as exc:
        logger.warning(f"[scanner] Could not fetch sector map: {exc}")
        sector_map = {}

    # 7–8d. Run all strategies (with optional rich progress bar)
    _strategies = [
        ("RSI",             lambda: rsi_strategy.generate_signals(tickers, today_indicators, regimes, open_tickers)),
        ("Momentum",        lambda: momentum_strategy.generate_signals(tickers, today_indicators, prev_indicators, regimes, open_tickers)),
        ("MACD",            lambda: macd_strategy.generate_signals(tickers, today_indicators, prev_indicators, regimes, open_tickers)),
        ("Reversal",        lambda: reversal_strategy.generate_signals(tickers, today_indicators, regimes, open_tickers, roc_rankings)),
        ("Sector rotation", lambda: sector_rotation_strategy.generate_signals(tickers, today_indicators, regimes, open_tickers, sector_scores, sector_map)),
    ]

    rsi_signals = mom_signals = macd_signals = reversal_signals = sector_signals = []

    if _RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Running strategies...", total=len(_strategies))
            results_list = []
            for name, fn in _strategies:
                progress.update(task, description=f"[cyan]{name}[/cyan]")
                results_list.append(fn())
                progress.advance(task)
    else:
        results_list = [fn() for _, fn in _strategies]

    rsi_signals, mom_signals, macd_signals, reversal_signals, sector_signals = results_list

    logger.info(
        f"[scanner] Signals — RSI:{len(rsi_signals)} Momentum:{len(mom_signals)} "
        f"MACD:{len(macd_signals)} Reversal:{len(reversal_signals)} "
        f"SectorRotation:{len(sector_signals)}"
    )

    # 9. Persist all signals (with Z-scores)
    all_signals = rsi_signals + mom_signals + macd_signals + reversal_signals + sector_signals
    if all_signals:
        _compute_z_scores(all_signals)
        try:
            signal_ids = insert_signals(all_signals, signal_time=trading_day)
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
        macd_signals=macd_signals,
        reversal_signals=reversal_signals,
        sector_signals=sector_signals,
        skipped_open_count=skipped_open_count,
        all_signals=all_signals,
    )

    logger.info("[scanner] Daily scan complete")


if __name__ == "__main__":
    """CLI: python -m strategies.scanner [--date YYYY-MM-DD]

    Without --date: scans the most recent trading day (live mode).
    With --date:    replays the scan for that historical date.
    """
    import sys

    _for_date = None
    _args = sys.argv[1:]
    _i = 0
    while _i < len(_args):
        if _args[_i] == "--date" and _i + 1 < len(_args):
            _for_date = datetime.strptime(_args[_i + 1], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            _i += 2
        elif _args[_i].startswith("--date="):
            _for_date = datetime.strptime(
                _args[_i].split("=", 1)[1], "%Y-%m-%d"
            ).replace(tzinfo=timezone.utc)
            _i += 1
        else:
            _i += 1

    run_daily_scan(for_date=_for_date)
