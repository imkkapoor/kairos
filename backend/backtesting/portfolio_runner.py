"""
backtesting/portfolio_runner.py — Portfolio-level backtester.

Simulates all strategies simultaneously on all tickers, applying
the same capital allocation logic as Phase 3:
  - Process all signals across all tickers for each day
  - Sort by strength DESC
  - Apply same risk limits (position size, exposure, sector caps)

This is the most accurate representation of how Kairos actually trades.
"""

import sys
import os
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from loguru import logger
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backtesting.config import (
    ATR_MULTIPLIER,
    INITIAL_CAPITAL,
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_RISK,
    MAX_POSITION_SIZE,
    MAX_SECTOR_EXPOSURE,
    MAX_TOTAL_EXPOSURE,
    MIN_SIGNAL_STRENGTH,
    TAKE_PROFIT_ATR_MULT,
    get_period_dates,
)
from backtesting.data_loader import load_combined, load_ticker_list
from db.connection import get_sector_map


def _clamp(value, lo, hi):
    return max(lo, min(value, hi))


def _compute_regime(adx):
    """Simplified per-ticker ADX-based regime."""
    if adx is None or adx != adx:
        return "CHOPPY", 0.3
    if adx > 25:
        return "TRENDING", _clamp((adx - 25) / 10, 0, 1)
    if adx < 20:
        return "CHOPPY", _clamp((20 - adx) / 8, 0, 1)
    return "CHOPPY", 0.3


def _generate_signals_for_bar(ticker, row, prev_row, strategy_name):
    """Generate signals from a single bar using simplified strategy logic.

    Returns a list of signal dicts (may be empty).
    """
    signals = []

    rsi = row.get("rsi_14")
    close = row.get("Close")
    bb_lower = row.get("bb_lower")
    bb_upper = row.get("bb_upper")
    atr = row.get("atr_14")
    adx = row.get("adx_14")
    volume = row.get("Volume")
    volume_sma = row.get("volume_sma")
    ma_50 = row.get("ma_50")
    ma_200 = row.get("ma_200")
    macd_line = row.get("macd_line")
    macd_signal = row.get("macd_signal")
    macd_hist = row.get("macd_hist")
    roc = row.get("roc_20")

    if atr is None or atr != atr or atr == 0:
        return signals

    vol_mult = _clamp(volume / volume_sma, 0.5, 1.5) if volume_sma and volume_sma > 0 else 1.0

    prev_ma_50 = prev_row.get("ma_50") if prev_row else None
    prev_ma_200 = prev_row.get("ma_200") if prev_row else None
    prev_macd = prev_row.get("macd_line") if prev_row else None
    prev_macd_signal = prev_row.get("macd_signal") if prev_row else None

    if strategy_name == "rsi":
        if rsi is not None and rsi == rsi and close is not None and bb_lower is not None:
            if rsi < 30 and close < bb_lower:
                base = _clamp((30 - rsi) / 15, 0, 1)
                strength = min(base * vol_mult, 1.0)
                if strength >= MIN_SIGNAL_STRENGTH:
                    signals.append({
                        "ticker": ticker, "strategy": "rsi", "signal_type": "buy",
                        "strength": strength, "atr": atr,
                    })
            elif rsi > 70 and bb_upper is not None and close > bb_upper:
                signals.append({
                    "ticker": ticker, "strategy": "rsi", "signal_type": "sell",
                    "strength": 0.5, "atr": atr,
                })

    elif strategy_name == "momentum":
        if all(v is not None and v == v for v in (ma_50, ma_200, prev_ma_50, prev_ma_200)):
            golden_cross = ma_50 > ma_200 and prev_ma_50 <= prev_ma_200
            death_cross = ma_50 < ma_200 and prev_ma_50 >= prev_ma_200
            if golden_cross:
                gap_pct = abs(ma_50 - ma_200) / ma_200 if ma_200 > 0 else 0
                base = _clamp(gap_pct * 10, 0.3, 1.0)
                strength = min(base * vol_mult, 1.0)
                if roc is not None and roc == roc:
                    if roc > 10:
                        strength = min(strength + 0.10, 1.0)
                    elif roc > 5:
                        strength = min(strength + 0.05, 1.0)
                if strength >= MIN_SIGNAL_STRENGTH:
                    signals.append({
                        "ticker": ticker, "strategy": "momentum", "signal_type": "buy",
                        "strength": strength, "atr": atr,
                    })
            elif death_cross:
                signals.append({
                    "ticker": ticker, "strategy": "momentum", "signal_type": "sell",
                    "strength": 0.5, "atr": atr,
                })

    elif strategy_name == "macd":
        if all(v is not None and v == v for v in (macd_line, macd_signal, prev_macd, prev_macd_signal)):
            if adx is not None and adx == adx and adx > 20:
                bullish = macd_line > macd_signal and prev_macd <= prev_macd_signal
                bearish = macd_line < macd_signal and prev_macd >= prev_macd_signal
                if bullish:
                    base = _clamp(abs(macd_hist) / (abs(macd_signal) + 1e-9), 0, 1)
                    strength = min(base * vol_mult, 1.0)
                    if strength >= MIN_SIGNAL_STRENGTH:
                        signals.append({
                            "ticker": ticker, "strategy": "macd", "signal_type": "buy",
                            "strength": strength, "atr": atr,
                        })
                elif bearish:
                    signals.append({
                        "ticker": ticker, "strategy": "macd", "signal_type": "sell",
                        "strength": 0.5, "atr": atr,
                    })

    elif strategy_name == "reversal":
        if roc is not None and roc == roc and rsi is not None and rsi == rsi:
            if roc < -5 and rsi > 20:
                base = _clamp(abs(roc) / 20, 0, 1)
                strength = min(base * vol_mult, 1.0)
                if strength >= MIN_SIGNAL_STRENGTH:
                    signals.append({
                        "ticker": ticker, "strategy": "reversal", "signal_type": "buy",
                        "strength": strength, "atr": atr,
                    })
            elif roc > 5:
                signals.append({
                    "ticker": ticker, "strategy": "reversal", "signal_type": "sell",
                    "strength": 0.5, "atr": atr,
                })

    return signals


def run_portfolio(strategies: list[str], period: str) -> dict:
    """Run a portfolio-level backtest simulating all strategies across all tickers.

    Applies the same capital allocation logic as Phase 3.
    Returns portfolio-level metrics.
    """
    start_date, end_date = get_period_dates(period)
    tickers = load_ticker_list()
    sector_map = get_sector_map()

    logger.info(f"Portfolio backtest: {len(tickers)} tickers, {len(strategies)} strategies, {period}")

    # Load all data upfront
    all_data = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
    ) as progress:
        task = progress.add_task("[cyan]Loading data...", total=len(tickers))
        for ticker in tickers:
            df = load_combined(ticker, start_date, end_date)
            if not df.empty and len(df) > 30:
                df.index = df.index.tz_localize(None)
                all_data[ticker] = df
            progress.advance(task)

    logger.info(f"Loaded data for {len(all_data)} tickers")
    if not all_data:
        return {"error": "no_data"}

    # Build unified date index from all tickers
    all_dates = sorted(set().union(*(df.index for df in all_data.values())))

    # Portfolio state
    cash = INITIAL_CAPITAL
    positions = {}  # ticker -> {qty, avg_cost, stop_loss, take_profit, strategy, bars_held}
    equity_curve = []
    trades = []
    peak_value = INITIAL_CAPITAL

    for i, date in enumerate(all_dates):
        # Compute portfolio value
        portfolio_value = cash
        for ticker, pos in list(positions.items()):
            if ticker in all_data and date in all_data[ticker].index:
                price = all_data[ticker].loc[date, "Close"]
                portfolio_value += pos["qty"] * price

        equity_curve.append({"date": date, "value": portfolio_value})
        peak_value = max(peak_value, portfolio_value)

        # --- Position management: check exits ---
        for ticker in list(positions.keys()):
            if ticker not in all_data or date not in all_data[ticker].index:
                continue

            pos = positions[ticker]
            price = all_data[ticker].loc[date, "Open"]  # Fill at Open
            pos["bars_held"] += 1

            # Stop loss
            if price <= pos["stop_loss"]:
                pnl = (price - pos["avg_cost"]) * pos["qty"]
                cash += pos["qty"] * price
                trades.append({
                    "ticker": ticker, "side": "sell", "price": price,
                    "qty": pos["qty"], "pnl": pnl, "reason": "stop_loss",
                    "strategy": pos["strategy"], "date": date,
                })
                del positions[ticker]
                continue

            # Take profit
            if price >= pos["take_profit"]:
                pnl = (price - pos["avg_cost"]) * pos["qty"]
                cash += pos["qty"] * price
                trades.append({
                    "ticker": ticker, "side": "sell", "price": price,
                    "qty": pos["qty"], "pnl": pnl, "reason": "take_profit",
                    "strategy": pos["strategy"], "date": date,
                })
                del positions[ticker]
                continue

            # Time-based exit for reversal (20 bars)
            if pos["strategy"] == "reversal" and pos["bars_held"] >= 20:
                pnl = (price - pos["avg_cost"]) * pos["qty"]
                cash += pos["qty"] * price
                trades.append({
                    "ticker": ticker, "side": "sell", "price": price,
                    "qty": pos["qty"], "pnl": pnl, "reason": "time_exit",
                    "strategy": pos["strategy"], "date": date,
                })
                del positions[ticker]
                continue

        # --- Generate all signals for this day ---
        all_signals = []
        for strategy_name in strategies:
            for ticker in all_data:
                if date not in all_data[ticker].index:
                    continue
                idx = all_data[ticker].index.get_loc(date)
                row = all_data[ticker].iloc[idx].to_dict()

                prev_row = None
                if idx > 0:
                    prev_row = all_data[ticker].iloc[idx - 1].to_dict()

                sigs = _generate_signals_for_bar(ticker, row, prev_row, strategy_name)
                all_signals.extend(sigs)

        # Process sell signals first
        for sig in all_signals:
            if sig["signal_type"] == "sell" and sig["ticker"] in positions:
                ticker = sig["ticker"]
                pos = positions[ticker]
                if ticker in all_data and date in all_data[ticker].index:
                    price = all_data[ticker].loc[date, "Close"]
                    pnl = (price - pos["avg_cost"]) * pos["qty"]
                    cash += pos["qty"] * price
                    trades.append({
                        "ticker": ticker, "side": "sell", "price": price,
                        "qty": pos["qty"], "pnl": pnl, "reason": f"signal_{sig['strategy']}",
                        "strategy": pos["strategy"], "date": date,
                    })
                    del positions[ticker]

        # Sort buy signals by strength DESC
        buy_signals = [s for s in all_signals if s["signal_type"] == "buy"]
        buy_signals.sort(key=lambda s: s["strength"], reverse=True)

        # Apply buy signals with risk limits
        for sig in buy_signals:
            ticker = sig["ticker"]
            if ticker in positions:
                continue
            if len(positions) >= MAX_OPEN_POSITIONS:
                break

            # Total exposure check
            total_exposure = sum(
                p["qty"] * all_data.get(t, pd.DataFrame()).loc[date, "Close"]
                if t in all_data and date in all_data[t].index else 0
                for t, p in positions.items()
            )
            if total_exposure / portfolio_value > MAX_TOTAL_EXPOSURE:
                break

            # Sector exposure check
            sector = sector_map.get(ticker, "Unknown")
            sector_exposure = sum(
                p["qty"] * all_data.get(t, pd.DataFrame()).loc[date, "Close"]
                if t in all_data and date in all_data[t].index else 0
                for t, p in positions.items()
                if sector_map.get(t, "Unknown") == sector
            )
            if portfolio_value > 0 and sector_exposure / portfolio_value > MAX_SECTOR_EXPOSURE:
                continue

            # Position sizing (ATR-based, same as Phase 3)
            atr = sig["atr"]
            # Use next bar's open if available, else current close
            next_idx = all_dates.index(date) + 1 if date in all_dates else None
            if next_idx and next_idx < len(all_dates):
                next_date = all_dates[next_idx]
                if ticker in all_data and next_date in all_data[ticker].index:
                    entry_price = all_data[ticker].loc[next_date, "Open"]
                else:
                    continue
            else:
                continue

            stop_price = entry_price - (ATR_MULTIPLIER * atr)
            risk_per_share = entry_price - stop_price
            if risk_per_share <= 0:
                continue

            dollar_risk = sig["strength"] * MAX_PORTFOLIO_RISK * portfolio_value
            size = int(dollar_risk / risk_per_share)
            if size < 1:
                continue
            if size * entry_price > MAX_POSITION_SIZE * portfolio_value:
                size = int((MAX_POSITION_SIZE * portfolio_value) / entry_price)
            if size < 1:
                continue
            if size * entry_price > cash:
                size = int(cash / entry_price)
            if size < 1:
                continue

            take_profit = entry_price + (TAKE_PROFIT_ATR_MULT * atr)
            cost = size * entry_price
            cash -= cost

            positions[ticker] = {
                "qty": size,
                "avg_cost": entry_price,
                "stop_loss": stop_price,
                "take_profit": take_profit,
                "strategy": sig["strategy"],
                "bars_held": 0,
            }
            trades.append({
                "ticker": ticker, "side": "buy", "price": entry_price,
                "qty": size, "pnl": 0, "reason": f"signal_{sig['strategy']}",
                "strategy": sig["strategy"], "date": next_date,
            })

    # Final portfolio value
    final_value = cash
    last_date = all_dates[-1] if all_dates else None
    for ticker, pos in positions.items():
        if ticker in all_data and last_date is not None and last_date in all_data[ticker].index:
            final_value += pos["qty"] * all_data[ticker].loc[last_date, "Close"]

    # Compute metrics
    equity_values = [e["value"] for e in equity_curve]
    total_pnl = final_value - INITIAL_CAPITAL

    # Drawdown
    peaks = pd.Series(equity_values).cummax()
    drawdowns = (pd.Series(equity_values) - peaks) / peaks * 100
    max_drawdown = abs(drawdowns.min()) if len(drawdowns) > 0 else 0

    # CAGR
    n_years = len(all_dates) / 252 if all_dates else 1
    cagr = (final_value / INITIAL_CAPITAL) ** (1 / n_years) - 1 if n_years > 0 else 0

    # Sharpe (annualized, daily returns)
    if len(equity_values) > 1:
        daily_returns = pd.Series(equity_values).pct_change().dropna()
        sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0
    else:
        sharpe = 0

    # Calmar
    calmar = cagr / (max_drawdown / 100) if max_drawdown > 0 else 0

    # Trade stats
    buy_trades = [t for t in trades if t["side"] == "buy"]
    sell_trades = [t for t in trades if t["side"] == "sell"]
    total_trades = len(sell_trades)
    winning = [t for t in sell_trades if t["pnl"] > 0]
    losing = [t for t in sell_trades if t["pnl"] <= 0]
    win_rate = len(winning) / total_trades if total_trades > 0 else 0
    avg_win = np.mean([t["pnl"] for t in winning]) if winning else 0
    avg_loss = np.mean([t["pnl"] for t in losing]) if losing else 0
    gross_profit = sum(t["pnl"] for t in winning)
    gross_loss = abs(sum(t["pnl"] for t in losing))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    return {
        "ticker": None,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "strategy": "portfolio",
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 4),
        "cagr": round(cagr, 4),
        "sharpe_ratio": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
        "max_drawdown": round(max_drawdown, 4),
        "final_value": round(final_value, 2),
        "total_pnl": round(total_pnl, 2),
        "equity_curve": equity_curve,
        "trades": trades,
    }
