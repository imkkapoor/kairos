"""
backtesting/run_backtest.py — Main entry point for the Kairos backtesting suite.

Called via: make backtest
  or: make backtest-single STRATEGY=rsi PERIOD=out_of_sample

Runs full backtest suite in order:
  1. Verify DB has sufficient data
  2. Run each strategy on in_sample period (all tickers)
  3. Run each strategy on out_of_sample period (all tickers)
  4. Store all results to backtest_results table
  5. Generate all charts
  6. Print summary report
"""

import argparse
import atexit
import math
import sys
import os

from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Restore stderr/stdout before Python tears down Rich's FileProxy objects
# (prevents "ImportError: sys.meta_path is None" noise during shutdown)
@atexit.register
def _restore_streams():
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__

from backtesting.config import (
    IN_SAMPLE_START,
    IN_SAMPLE_END,
    OUT_SAMPLE_START,
    OUT_SAMPLE_END,
    INITIAL_CAPITAL,
    get_period_dates,
)
from backtesting.data_loader import load_ticker_list
from backtesting.runner import run_all_tickers
from backtesting.charts import (
    plot_equity_curves,
    plot_drawdown,
    plot_monthly_returns_heatmap,
    plot_strategy_comparison,
)
from db.connection import ping

# Strategy registry: name -> backtesting.py Strategy class
STRATEGY_MAP = {}


def _load_strategies():
    """Lazy-load strategy classes to avoid import failures if backtesting not installed."""
    global STRATEGY_MAP
    if STRATEGY_MAP:
        return
    from backtesting.strategies.bt_rsi import RSIStrategy
    from backtesting.strategies.bt_momentum import MomentumStrategy
    from backtesting.strategies.bt_macd import MACDStrategy
    from backtesting.strategies.bt_reversal import ReversalStrategy

    STRATEGY_MAP = {
        "rsi": RSIStrategy,
        "momentum": MomentumStrategy,
        "macd": MACDStrategy,
        "reversal": ReversalStrategy,
        # sector_rotation requires cross-sectional data; excluded from single-ticker mode.
        # Add it back in portfolio_runner.py when multi-ticker support is built.
    }


def _store_results(results: list[dict], strategy_name: str, mode: str = "single_ticker"):
    """Store backtest results to the backtest_results DB table."""
    from db.connection import insert_backtest_result

    stored = 0
    for r in results:
        if "error" in r:
            continue
        try:
            insert_backtest_result({
                "strategy": strategy_name,
                "ticker": r.get("ticker"),
                "period": r["period"],
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "total_trades": r["total_trades"],
                "win_rate": r.get("win_rate"),
                "avg_win": r.get("avg_win"),
                "avg_loss": r.get("avg_loss"),
                "profit_factor": r.get("profit_factor"),
                "cagr": r.get("cagr"),
                "sharpe_ratio": r.get("sharpe_ratio"),
                "calmar_ratio": r.get("calmar_ratio"),
                "max_drawdown": r.get("max_drawdown"),
                "final_value": r.get("final_value"),
                "total_pnl": r.get("total_pnl"),
                "mode": mode,
                "params": None,
                "notes": None,
            })
            stored += 1
        except Exception as exc:
            logger.warning(f"Failed to store result for {r.get('ticker')}: {exc}")


def _compute_strategy_summary(results: list[dict]) -> dict:
    """Compute average metrics across all tickers for a strategy."""
    if not results:
        return {}

    import numpy as np

    valid = [r for r in results if "error" not in r and r.get("total_trades", 0) > 0]
    if not valid:
        return {}

    def _avg(key):
        vals = [r.get(key) for r in valid]
        nums = [float(v) for v in vals
                if v is not None and not math.isnan(float(v)) and not math.isinf(float(v))]
        return float(np.mean(nums)) if nums else 0.0

    return {
        "sharpe_ratio": _avg("sharpe_ratio"),
        "calmar_ratio": _avg("calmar_ratio"),
        "win_rate": _avg("win_rate"),
        "max_drawdown": _avg("max_drawdown"),
        "total_trades": sum(r.get("total_trades", 0) for r in valid),
        "cagr": _avg("cagr"),
        "profit_factor": _avg("profit_factor"),
        "avg_win": _avg("avg_win"),
        "avg_loss": _avg("avg_loss"),
        "tickers_tested": len(valid),
    }


def _print_summary(strategy_summaries: dict, period: str, n_tickers: int):
    """Print the summary report."""
    start, end = get_period_dates(period)

    W = 82
    print()
    print("=" * W)
    print("=== Kairos Backtest Results ===")
    print(f"Period: {start} to {end} ({period})")
    print(f"Tickers tested: {n_tickers}")
    print()
    print(f"{'Strategy':<18} {'Sharpe':>7} {'Calmar':>7} {'Win%':>6} {'PF':>6} {'AvgW%':>6} {'AvgL%':>6} {'Trades':>7} {'CAGR':>6}")
    print("-" * W)

    for name, s in strategy_summaries.items():
        if not s:
            print(f"{name:<18} {'N/A':>7} {'N/A':>7} {'N/A':>6} {'N/A':>6} {'N/A':>6} {'N/A':>6} {'N/A':>7} {'N/A':>6}")
            continue
        print(
            f"{name:<18} "
            f"{s.get('sharpe_ratio', 0):>7.2f} "
            f"{s.get('calmar_ratio', 0):>7.2f} "
            f"{s.get('win_rate', 0) * 100:>5.1f}% "
            f"{s.get('profit_factor', 0):>6.2f} "
            f"{s.get('avg_win', 0):>5.1f}% "
            f"{s.get('avg_loss', 0):>5.1f}% "
            f"{s.get('total_trades', 0):>7} "
            f"{s.get('cagr', 0) * 100:>5.1f}%"
        )

    print()
    print("  * CAGR is the annualised return per ticker, averaged across all tickers.")
    print("    In single-ticker mode each backtest deploys ~99% of $100k capital,")
    print("    so CAGR reflects the real compounded edge of the strategy on that stock.")
    print("    Key metrics to focus on: Sharpe, Win Rate, and Profit Factor")
    print("    (PF > 1.0 = profitable edge, PF > 1.5 = strong).")

    # Best strategies (by Sharpe, then profit_factor as tiebreak)
    active = {n: s for n, s in strategy_summaries.items() if s}
    if active:
        best_sharpe = max(active, key=lambda n: active[n].get("sharpe_ratio", 0))
        best_pf = max(active, key=lambda n: active[n].get("profit_factor", 0))
        print()
        print(f"  Best by Sharpe ({period}):         {best_sharpe}")
        print(f"  Best by Profit Factor ({period}):  {best_pf}")

    print()
    print(f"  Charts saved to: backend/backtesting/output/")
    print("=" * W)
    print()


def run_full_suite():
    """Run the complete backtest suite: all strategies, both periods."""
    _load_strategies()

    # 1. Verify DB
    if not ping():
        logger.error("Cannot connect to database. Run 'make up' first.")
        sys.exit(1)

    tickers = load_ticker_list()
    if not tickers:
        logger.error("No tickers with sufficient data. Run setup first.")
        sys.exit(1)
    print(f"Kairos Backtesting Suite — single_ticker mode")
    print()

    all_summaries = {}

    for period in ("in_sample", "out_of_sample"):
        start, end = get_period_dates(period)
        period_tickers = load_ticker_list(start_date=start)
        print(f"─── {period}  ({start} → {end})  [{len(period_tickers)} tickers] ───────────────────────────")

        period_summaries = {}

        for name, strategy_class in STRATEGY_MAP.items():
            results = run_all_tickers(strategy_class, name, period,
                                      single_ticker_mode=True)

            # Store to DB
            _store_results(results, name, mode="single_ticker")

            # Generate charts
            plot_equity_curves(results, name, period)
            plot_drawdown(results, name, period)
            plot_monthly_returns_heatmap(results, name)

            # Compute summary
            summary = _compute_strategy_summary(results)
            period_summaries[name] = summary

        # Strategy comparison chart
        plot_strategy_comparison(period_summaries, period)
        print()

        if period == "out_of_sample":
            all_summaries = period_summaries

    # Print final summary (out-of-sample is the honest score)
    out_tickers = load_ticker_list(start_date=OUT_SAMPLE_START)
    _print_summary(all_summaries, "out_of_sample", len(out_tickers))


def run_single_strategy(strategy_name: str, period: str):
    """Run backtest for a single strategy and period."""
    _load_strategies()

    if strategy_name not in STRATEGY_MAP:
        logger.error(f"Unknown strategy: {strategy_name}. Available: {list(STRATEGY_MAP.keys())}")
        sys.exit(1)

    if not ping():
        logger.error("Cannot connect to database. Run 'make up' first.")
        sys.exit(1)

    start, end = get_period_dates(period)
    tickers = load_ticker_list(start_date=start)
    print(f"Kairos Backtesting — {strategy_name} | {period} ({start} → {end}) | {len(tickers)} tickers")
    print()

    strategy_class = STRATEGY_MAP[strategy_name]
    results = run_all_tickers(strategy_class, strategy_name, period,
                              single_ticker_mode=True)

    _store_results(results, strategy_name, mode="single_ticker")
    plot_equity_curves(results, strategy_name, period)
    plot_drawdown(results, strategy_name, period)
    plot_monthly_returns_heatmap(results, strategy_name)

    summary = _compute_strategy_summary(results)
    _print_summary({strategy_name: summary}, period, len(tickers))


def main():
    # Suppress INFO-level logs during bulk runs; Rich progress bars handle user feedback
    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="{level}: {message}")

    parser = argparse.ArgumentParser(description="Kairos Backtesting Suite")
    parser.add_argument("--strategy", type=str, default=None,
                        help="Run a single strategy (rsi, momentum, macd, reversal, sector_rotation)")
    parser.add_argument("--period", type=str, default=None,
                        help="Period to test (in_sample, out_of_sample, full)")
    args = parser.parse_args()

    if args.strategy and args.period:
        run_single_strategy(args.strategy, args.period)
    elif args.strategy or args.period:
        logger.error("Must specify both --strategy and --period, or neither for full suite")
        sys.exit(1)
    else:
        run_full_suite()


if __name__ == "__main__":
    main()
