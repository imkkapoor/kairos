"""
backtesting/runner.py — Single-ticker backtester.

Runs a backtesting.py Strategy subclass on one ticker at a time.
Stateless — does NOT write results to DB. Returns metric dicts.
"""

import io
import math
import os
import sys
import warnings

from loguru import logger
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backtesting._lib import Backtest
from backtesting.config import (
    COMMISSION,
    INITIAL_CAPITAL,
    get_period_dates,
)
from backtesting.data_loader import load_combined, load_ticker_list


def _s(stats, key: str, default: float = 0.0) -> float:
    """Safely extract a scalar from a backtesting.py stats Series.

    Handles NaN/inf/None — all collapse to `default`.
    NaN is truthy in Python so the common `val or 0` idiom silently passes NaN
    through; this helper avoids that trap.
    """
    val = stats.get(key, default)
    if val is None:
        return default
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def run_single(ticker: str, strategy_class, period: str,
               single_ticker_mode: bool = True) -> dict:
    """Run a backtest on a single ticker for the given period.

    Args:
        single_ticker_mode: When True (default), deploys ~99% of equity per
            trade so results reflect the strategy's edge without idle-cash
            dilution. Set False for portfolio-allocation mode.

    Returns a dict of performance metrics. Does NOT save to DB.
    """
    start_date, end_date = get_period_dates(period)
    data = load_combined(ticker, start_date, end_date)

    if data.empty or len(data) < 30:
        return {"ticker": ticker, "period": period, "error": "insufficient_data"}

    # backtesting.py requires no timezone on the index
    data.index = data.index.tz_localize(None)

    run_kwargs = {}
    if single_ticker_mode:
        run_kwargs["invest_fraction"] = 0.99

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        bt = Backtest(
            data,
            strategy_class,
            cash=INITIAL_CAPITAL,
            commission=COMMISSION,
            exclusive_orders=True,
        )
        # Suppress backtesting.py's own tqdm bar by redirecting stderr
        _stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            stats = bt.run(**run_kwargs)
        finally:
            sys.stderr = _stderr

    # Extract metrics from backtesting.py stats object
    total_trades = stats.get("# Trades", 0)
    if total_trades == 0:
        return {
            "ticker": ticker,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "total_trades": 0,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
            "cagr": None,
            "sharpe_ratio": None,
            "calmar_ratio": None,
            "max_drawdown": None,
            "final_value": stats.get("Equity Final [$]", INITIAL_CAPITAL),
            "total_pnl": stats.get("Equity Final [$]", INITIAL_CAPITAL) - INITIAL_CAPITAL,
        }

    sharpe_ratio = _s(stats, "Sharpe Ratio")
    max_drawdown = abs(_s(stats, "Max. Drawdown [%]"))
    cagr = _s(stats, "Return (Ann.) [%]") / 100
    calmar_ratio = cagr / (max_drawdown / 100) if max_drawdown > 0 else 0.0
    win_rate = _s(stats, "Win Rate [%]") / 100
    profit_factor = _s(stats, "Profit Factor")
    final_value = _s(stats, "Equity Final [$]", INITIAL_CAPITAL)
    total_pnl = final_value - INITIAL_CAPITAL

    # Compute avg win/loss from _trades directly — backtesting.py does not
    # expose "Avg. Winning Trade [%]" / "Avg. Losing Trade [%]" as stat keys.
    trades_df = stats.get("_trades")
    if trades_df is not None and not trades_df.empty and "ReturnPct" in trades_df.columns:
        winning = trades_df.loc[trades_df["ReturnPct"] > 0, "ReturnPct"] * 100
        losing  = trades_df.loc[trades_df["ReturnPct"] < 0, "ReturnPct"] * 100
        avg_win  = float(winning.mean()) if not winning.empty else 0.0
        avg_loss = float(losing.mean())  if not losing.empty  else 0.0
        # Sanity-check: recompute PF from trades if stats PF was zero
        if profit_factor == 0.0 and avg_loss != 0.0:
            gross_profit = float(winning.sum())
            gross_loss   = abs(float(losing.sum()))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
    else:
        avg_win = avg_loss = 0.0

    return {
        "ticker": ticker,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 4),
        "cagr": round(cagr, 4),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "calmar_ratio": round(calmar_ratio, 4),
        "max_drawdown": round(max_drawdown, 4),
        "final_value": round(final_value, 2),
        "total_pnl": round(total_pnl, 2),
    }


def run_all_tickers(strategy_class, strategy_name: str, period: str,
                    single_ticker_mode: bool = True) -> list[dict]:
    """Run a backtest on all eligible tickers for a given strategy and period.

    Returns list of result dicts. Shows a rich progress bar.
    """
    start_date, _ = get_period_dates(period)
    tickers = load_ticker_list(min_rows=100, start_date=start_date)
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
    ) as progress:
        task = progress.add_task(
            f"[cyan]{strategy_name} ({period})", total=len(tickers)
        )

        for ticker in tickers:
            try:
                result = run_single(ticker, strategy_class, period,
                                    single_ticker_mode=single_ticker_mode)
                if "error" not in result:
                    result["strategy"] = strategy_name
                    results.append(result)
            except Exception as exc:
                logger.warning(f"{strategy_name}/{ticker}: backtest failed — {exc}")
            progress.advance(task)

    return results
