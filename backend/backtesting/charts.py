"""
backtesting/charts.py — Matplotlib charts for backtest results.

Saves all charts as PNG to backend/backtesting/output/.
Creates output/ dir if missing.
"""

import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server/CI
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def plot_equity_curves(results: list[dict], strategy_name: str, period: str):
    """Plot equity curves — one line per ticker with average in bold.

    Expects each result dict to have 'ticker' and 'final_value' keys.
    Since backtesting.py doesn't return per-bar equity by default for
    single-ticker runs, we plot final values as a bar chart instead.
    """
    if not results:
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    tickers = [r["ticker"] for r in results]
    final_values = [r.get("final_value", 100_000) for r in results]
    initial = 100_000

    # Color bars: green if profit, red if loss
    colors = ["#2ecc71" if v > initial else "#e74c3c" for v in final_values]

    # Show top/bottom 30 tickers by P&L for readability
    sorted_results = sorted(zip(tickers, final_values, colors), key=lambda x: x[1], reverse=True)
    if len(sorted_results) > 60:
        show = sorted_results[:30] + sorted_results[-30:]
    else:
        show = sorted_results

    tickers_show = [s[0] for s in show]
    values_show = [s[1] for s in show]
    colors_show = [s[2] for s in show]

    ax.barh(range(len(tickers_show)), values_show, color=colors_show, height=0.7)
    ax.set_yticks(range(len(tickers_show)))
    ax.set_yticklabels(tickers_show, fontsize=6)
    ax.axvline(x=initial, color="black", linestyle="--", alpha=0.5, label="Initial Capital")
    ax.set_xlabel("Final Portfolio Value ($)")
    ax.set_title(f"{strategy_name} — Final Values ({period})")
    ax.legend()

    plt.tight_layout()
    path = OUTPUT_DIR / f"{strategy_name}_{period}_equity_curves.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_drawdown(results: list[dict], strategy_name: str, period: str):
    """Plot max drawdown per ticker as a horizontal bar chart."""
    if not results:
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    data = [(r["ticker"], r.get("max_drawdown", 0)) for r in results if r.get("max_drawdown") is not None]
    data.sort(key=lambda x: x[1], reverse=True)

    # Top 40 drawdowns
    data = data[:40]
    tickers = [d[0] for d in data]
    drawdowns = [d[1] for d in data]

    ax.barh(range(len(tickers)), drawdowns, color="#e74c3c", height=0.7)
    ax.set_yticks(range(len(tickers)))
    ax.set_yticklabels(tickers, fontsize=7)
    ax.set_xlabel("Max Drawdown (%)")
    ax.set_title(f"{strategy_name} — Max Drawdown ({period})")

    plt.tight_layout()
    path = OUTPUT_DIR / f"{strategy_name}_{period}_drawdown.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_monthly_returns_heatmap(results: list[dict], strategy_name: str):
    """Month x Year heatmap of average returns across all tickers.

    Uses total_pnl / initial_capital as a proxy for monthly return.
    Groups by ticker count per period since we don't have monthly granularity
    from single-ticker runs.
    """
    if not results:
        return

    # Aggregate: compute average CAGR as a proxy for return performance
    cagrs = [r.get("cagr", 0) for r in results if r.get("cagr") is not None]
    if not cagrs:
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    # Distribution of CAGR values
    ax.hist(cagrs, bins=30, color="#3498db", edgecolor="black", alpha=0.8)
    avg_cagr = np.mean(cagrs)
    ax.axvline(avg_cagr, color="red", linestyle="--", label=f"Mean CAGR: {avg_cagr:.2%}")
    ax.set_xlabel("CAGR")
    ax.set_ylabel("Number of Tickers")
    ax.set_title(f"{strategy_name} — CAGR Distribution")
    ax.legend()

    plt.tight_layout()
    path = OUTPUT_DIR / f"{strategy_name}_cagr_distribution.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_strategy_comparison(strategy_summaries: dict, period: str = "out_of_sample"):
    """Bar chart comparing strategies by Sharpe and Calmar ratios.

    strategy_summaries: {strategy_name: {sharpe_ratio, calmar_ratio, win_rate, ...}}
    """
    if not strategy_summaries:
        return

    strategies = list(strategy_summaries.keys())
    sharpes = [strategy_summaries[s].get("sharpe_ratio", 0) or 0 for s in strategies]
    calmars = [strategy_summaries[s].get("calmar_ratio", 0) or 0 for s in strategies]

    x = np.arange(len(strategies))
    width = 0.35

    fig, ax1 = plt.subplots(figsize=(12, 6))

    bars1 = ax1.bar(x - width / 2, sharpes, width, label="Sharpe Ratio", color="#3498db", alpha=0.8)
    bars2 = ax1.bar(x + width / 2, calmars, width, label="Calmar Ratio", color="#2ecc71", alpha=0.8)

    ax1.set_xlabel("Strategy")
    ax1.set_ylabel("Ratio")
    ax1.set_title(f"Strategy Comparison — {period}")
    ax1.set_xticks(x)
    ax1.set_xticklabels(strategies)
    ax1.legend()
    ax1.axhline(y=0, color="black", linewidth=0.8)

    # Add value labels
    for bar in bars1:
        h = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2., h, f"{h:.2f}",
                 ha="center", va="bottom", fontsize=8)
    for bar in bars2:
        h = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2., h, f"{h:.2f}",
                 ha="center", va="bottom", fontsize=8)

    plt.tight_layout()
    path = OUTPUT_DIR / f"strategy_comparison_{period}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_portfolio_equity(equity_curve: list[dict], period: str):
    """Plot portfolio equity curve from portfolio_runner results."""
    if not equity_curve:
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    dates = [e["date"] for e in equity_curve]
    values = [e["value"] for e in equity_curve]

    ax.plot(dates, values, color="#3498db", linewidth=1.5)
    ax.axhline(y=100_000, color="gray", linestyle="--", alpha=0.5, label="Initial Capital")
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio Value ($)")
    ax.set_title(f"Portfolio Equity Curve ({period})")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = OUTPUT_DIR / f"portfolio_{period}_equity.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
