"""
simulator/position_manager.py — Checks all open positions for exit conditions.

Runs BEFORE executor each cycle. Returns actions for simulator.py to execute.
No DB writes here — returns data structures that simulator.py acts on.

Exit priority (first match wins per ticker):
  1. Stop loss
  2. Take profit
  3. Trailing stop update (does NOT close — updates stop_loss in portfolio + DB)
  4. Signal reversal (RSI sell | momentum death cross)
  5. Time-based exit (> 30 days open, negative P&L)
"""

import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)

_ATR_MULTIPLIER = float(os.environ.get("ATR_MULTIPLIER", 2.0))


def check_positions(
    portfolio,              # Portfolio instance
    current_prices: dict,
    today_indicators: dict,
    todays_signals: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Evaluate all open positions for exit / trailing stop conditions.

    Parameters
    ----------
    portfolio:
        Live Portfolio object. Trailing stop updates mutate positions in place.
    current_prices:
        {ticker: float} fill prices for today.
    today_indicators:
        {ticker: {indicator_col: value}} from get_todays_indicators().
    todays_signals:
        List of today's signal dicts (already converted from DataFrame).

    Returns
    -------
    (close_actions, trailing_stop_updates)
        close_actions:
            list of {ticker, exit_price, reason}
        trailing_stop_updates:
            list of {ticker, new_stop_loss}
            simulator.py iterates this and calls update_stop_loss() in DB.
    """
    atr_multiplier = float(os.environ.get("ATR_MULTIPLIER", _ATR_MULTIPLIER))

    # Build a fast lookup for today's sell signals: {ticker: signal_dict}
    sell_signals: dict[str, dict] = {}
    for sig in todays_signals:
        if str(sig.get("signal_type", "")).lower() == "sell":
            ticker = sig.get("ticker", "")
            if ticker:
                sell_signals[ticker] = sig

    close_actions:         list[dict] = []
    trailing_stop_updates: list[dict] = []
    tickers_to_close:      set[str]   = set()

    for ticker, pos in list(portfolio.positions.items()):
        price     = current_prices.get(ticker, pos["avg_cost"])
        stop_loss = pos.get("stop_loss", 0.0)
        take_profit = pos.get("take_profit", float("inf"))
        strategy  = pos.get("strategy", "")
        atr_today = today_indicators.get(ticker, {}).get("atr_14")

        # ------------------------------------------------------------------
        # 1. Stop loss
        # ------------------------------------------------------------------
        if price <= stop_loss:
            reason = f"Stop loss hit at ${price:.2f} (SL: ${stop_loss:.2f})"
            close_actions.append({"ticker": ticker, "exit_price": price, "reason": reason})
            tickers_to_close.add(ticker)
            logger.info(f"STOP LOSS  {ticker}: {reason}")
            continue

        # ------------------------------------------------------------------
        # 2. Take profit
        # ------------------------------------------------------------------
        if price >= take_profit:
            reason = f"Take profit hit at ${price:.2f} (TP: ${take_profit:.2f})"
            close_actions.append({"ticker": ticker, "exit_price": price, "reason": reason})
            tickers_to_close.add(ticker)
            logger.info(f"TAKE PROFIT {ticker}: {reason}")
            continue

        # ------------------------------------------------------------------
        # 3. Trailing stop update (only if ATR available and in profit > 5%)
        # ------------------------------------------------------------------
        if atr_today is not None and atr_today != 0:
            if price > pos["avg_cost"] * 1.05:
                new_stop = price - (atr_multiplier * atr_today)
                if new_stop > stop_loss:
                    portfolio.positions[ticker]["stop_loss"] = new_stop
                    trailing_stop_updates.append(
                        {"ticker": ticker, "new_stop_loss": round(new_stop, 2)}
                    )
                    logger.info(
                        f"TRAIL STOP {ticker}: updated SL ${stop_loss:.2f} → ${new_stop:.2f}"
                    )
        elif atr_today is None or atr_today == 0:
            logger.debug(
                f"Trailing stop skipped for {ticker}: ATR missing/zero — "
                "stop-loss/take-profit still checked above"
            )

        # ------------------------------------------------------------------
        # 4. Signal reversal
        # ------------------------------------------------------------------
        reversal_triggered = False

        # RSI reversal: sell signal with strength > 0.5
        if strategy == "rsi" and ticker in sell_signals:
            sig_strength = float(sell_signals[ticker].get("strength", 0.0))
            if sig_strength > 0.5:
                reason = f"RSI sell signal (strength {sig_strength:.2f}) while position open"
                close_actions.append({"ticker": ticker, "exit_price": price, "reason": reason})
                tickers_to_close.add(ticker)
                logger.info(f"RSI REVERSAL {ticker}: {reason}")
                reversal_triggered = True

        # Momentum reversal: death cross (ma_50 < ma_200)
        if not reversal_triggered and strategy == "momentum":
            ind = today_indicators.get(ticker, {})
            ma_50  = ind.get("ma_50")
            ma_200 = ind.get("ma_200")
            if ma_50 is not None and ma_200 is not None and ma_50 < ma_200:
                reason = "Death cross while momentum position open"
                close_actions.append({"ticker": ticker, "exit_price": price, "reason": reason})
                tickers_to_close.add(ticker)
                logger.info(f"DEATH CROSS {ticker}: {reason}")
                reversal_triggered = True

        if reversal_triggered:
            continue

        # ------------------------------------------------------------------
        # 5. Time-based exit (> 30 days open and in loss)
        # ------------------------------------------------------------------
        try:
            opened_at = datetime.fromisoformat(pos["opened_at"])
            age = datetime.now(timezone.utc) - opened_at
            pnl = (price - pos["avg_cost"]) * pos["qty"]
            if age.days > 30 and pnl < 0:
                reason = f"Time exit: {age.days}d open, P&L ${pnl:.2f}"
                close_actions.append({"ticker": ticker, "exit_price": price, "reason": reason})
                tickers_to_close.add(ticker)
                logger.info(f"TIME EXIT  {ticker}: {reason}")
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(f"Time-exit check for {ticker}: could not parse opened_at — {exc}")

    return close_actions, trailing_stop_updates
