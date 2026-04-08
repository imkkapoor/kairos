"""
simulator/executor.py — Converts strategy signals into paper trades.

Rules:
  - No DB access of any kind — all data is passed in by simulator.py.
  - sector_map is pre-fetched by simulator.py and passed in; do NOT query DB here.
  - Returns (executed, skipped) lists for simulator.py to persist.
"""

import os
from math import floor
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)

_MAX_PORTFOLIO_RISK    = float(os.environ.get("MAX_PORTFOLIO_RISK", 0.02))
_ATR_MULTIPLIER        = float(os.environ.get("ATR_MULTIPLIER", 2.0))
_TAKE_PROFIT_ATR_MULT  = float(os.environ.get("TAKE_PROFIT_ATR_MULT", 3.0))

import yfinance as yf
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_MARKET_OPEN_HOUR   = int(os.environ.get("MARKET_OPEN_HOUR_ET",   9))
_MARKET_OPEN_MINUTE = int(os.environ.get("MARKET_OPEN_MINUTE_ET", 31))


def _open_cutoff_utc() -> datetime:
    """Return today's market-open time (default 9:31 AM ET) as a UTC datetime.

    Used to pin fill prices to the opening bar so manual and scheduled runs
    produce identical fills regardless of when the job actually executes.
    """
    now_et = datetime.now(_ET)
    cutoff_et = now_et.replace(
        hour=_MARKET_OPEN_HOUR,
        minute=_MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0,
    )
    return cutoff_et.astimezone(timezone.utc)


def fetch_live_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch the 9:31 AM ET opening bar price for all tickers in one batch call.

    Downloads 1-day 1-minute bars and selects the last bar at or before
    9:31 AM ET (configurable via MARKET_OPEN_HOUR_ET / MARKET_OPEN_MINUTE_ET).
    This pins fills to the opening bar so manual runs (make simulate) and the
    scheduled 9:31 AM job produce consistent prices.

    If the market has not yet opened (e.g. run before 9:31 AM ET), the cutoff
    falls before all bars and no prices are returned — trades are skipped.

    A single yf.download call is used for all tickers to avoid rate limiting.
    Canadian tickers (e.g. RY.TO) are handled normally by yfinance==1.2.0.

    Returns {ticker: price}. Tickers with no bar at or before the cutoff are
    excluded with a logged warning.
    """
    if not tickers:
        return {}

    cutoff_utc = _open_cutoff_utc()
    logger.info(f"fetch_live_prices: cutoff {cutoff_utc.strftime('%Y-%m-%d %H:%M UTC')} ({len(tickers)} tickers)")

    result: dict[str, float] = {}
    try:
        logger.debug(f"fetch_live_prices: → yfinance 1m period=1d ({len(tickers)} tickers)")
        df = yf.download(
            tickers,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            logger.warning("fetch_live_prices: yfinance returned empty DataFrame")
            return {}
        logger.debug(f"fetch_live_prices: ← yfinance {len(df)} bars, {df.shape[1]} cols")

        # Normalise index to UTC
        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        # Keep only bars at or before the 9:31 AM ET cutoff
        df = df[df.index <= cutoff_utc]
        if df.empty:
            logger.warning(
                f"fetch_live_prices: no bars at or before cutoff "
                f"{cutoff_utc.strftime('%H:%M UTC')} — market may not be open yet"
            )
            return {}

        close = df["Close"]

        def _extract(series) -> Optional[float]:
            series = series.dropna()
            if series.empty:
                return None
            price = float(series.iloc[-1])
            return price if price > 0 else None

        # Single ticker returns a Series; multiple tickers return a DataFrame
        if hasattr(close, "columns"):
            for ticker in tickers:
                try:
                    price = _extract(close[ticker])
                    if price is None:
                        logger.warning(f"fetch_live_prices: no data at cutoff for {ticker}")
                    else:
                        result[ticker] = price
                except Exception as exc:
                    logger.warning(f"fetch_live_prices: error extracting {ticker}: {exc}")
        else:
            ticker = tickers[0]
            price = _extract(close)
            if price is None:
                logger.warning(f"fetch_live_prices: no data at cutoff for {ticker}")
            else:
                result[ticker] = price

    except Exception as exc:
        logger.warning(f"fetch_live_prices: batch download failed: {exc}")

    return result


def fetch_current_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch the most recent available 1-minute bar price for each ticker.

    Used by intraday position management (hourly checks). Unlike
    fetch_live_prices(), there is no 9:31 AM ET cutoff — the latest bar
    at or before *now* is used.
    """
    if not tickers:
        return {}

    cutoff_utc = datetime.now(timezone.utc)
    logger.info(f"fetch_current_prices: {len(tickers)} tickers at {cutoff_utc.strftime('%H:%M UTC')}")

    result: dict[str, float] = {}
    try:
        logger.debug(f"fetch_current_prices: → yfinance 1m period=1d ({len(tickers)} tickers)")
        df = yf.download(
            tickers,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            logger.warning("fetch_current_prices: yfinance returned empty DataFrame")
            return {}
        logger.debug(f"fetch_current_prices: ← yfinance {len(df)} bars, {df.shape[1]} cols")

        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        df = df[df.index <= cutoff_utc]
        if df.empty:
            logger.warning("fetch_current_prices: no bars at or before now")
            return {}

        close = df["Close"]

        def _extract(series) -> Optional[float]:
            series = series.dropna()
            if series.empty:
                return None
            price = float(series.iloc[-1])
            return price if price > 0 else None

        if hasattr(close, "columns"):
            for ticker in tickers:
                try:
                    price = _extract(close[ticker])
                    if price is None:
                        logger.warning(f"fetch_current_prices: no data for {ticker}")
                    else:
                        result[ticker] = price
                except Exception as exc:
                    logger.warning(f"fetch_current_prices: error extracting {ticker}: {exc}")
        else:
            ticker = tickers[0]
            price = _extract(close)
            if price is None:
                logger.warning(f"fetch_current_prices: no data for {ticker}")
            else:
                result[ticker] = price

    except Exception as exc:
        logger.warning(f"fetch_current_prices: batch download failed: {exc}")

    return result


# ---------------------------------------------------------------------------
# Currency helpers
# ---------------------------------------------------------------------------

_PORTFOLIO_CURRENCY = os.environ.get("PORTFOLIO_CURRENCY", "CAD")

# Map market codes (from watchlist.market) to native currencies
_MARKET_CURRENCY = {"US": "USD", "CA": "CAD"}


def get_ticker_currency(ticker: str, market_map: dict[str, str]) -> str:
    """Return the native currency for a ticker based on its market.

    market_map: {ticker: market_code} pre-fetched from watchlist.
    """
    market = market_map.get(ticker, "US")
    return _MARKET_CURRENCY.get(market, "USD")


def fetch_fx_rate(from_ccy: str, to_ccy: str) -> float:
    """Fetch the FX rate from_ccy→to_ccy at the 9:31 AM ET bar via yfinance.

    Uses the same _open_cutoff_utc() as fetch_live_prices so that stock
    fills and FX conversions reference the same point in time.
    Returns 1.0 if from_ccy == to_ccy.
    Returns None on failure (caller should handle).
    """
    if from_ccy == to_ccy:
        return 1.0

    pair = f"{from_ccy}{to_ccy}=X"  # e.g. USDCAD=X
    cutoff_utc = _open_cutoff_utc()
    logger.info(f"fetch_fx_rate: {pair} cutoff {cutoff_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    try:
        # period=5d so we have data even if today's bars haven't arrived yet
        df = yf.download(pair, period="5d", interval="1m", progress=False)
        if df is None or df.empty:
            logger.warning(f"fetch_fx_rate: no data for {pair}")
            return None

        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York").tz_convert("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        filtered = df[df.index <= cutoff_utc]
        if filtered.empty:
            # Cutoff is before all bars (e.g. running before market open or
            # on a weekend).  Fall back to the most recent bar available and
            # log a warning so the caller knows the rate isn't from 9:31.
            logger.warning(
                f"fetch_fx_rate: no bars at or before cutoff for {pair} — "
                f"using most recent available bar as fallback"
            )
            filtered = df

        close = filtered["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        series = close.dropna()
        if series.empty:
            logger.warning(f"fetch_fx_rate: no close data for {pair}")
            return None

        rate = float(series.iloc[-1])
        logger.info(f"fetch_fx_rate: {pair} = {rate:.4f}")
        return rate if rate > 0 else None

    except Exception as exc:
        logger.warning(f"fetch_fx_rate({pair}): {exc}")
        return None


def execute_signals(
    signals: list[dict],
    portfolio,              # Portfolio instance — no direct import to avoid circular
    current_prices: dict,
    today_indicators: dict,
    sector_map: dict[str, str],
    market_map: Optional[dict[str, str]] = None,
    fx_rates: Optional[dict[str, float]] = None,
) -> tuple[list[dict], list[dict]]:
    """Process a list of signals (sorted by z_score DESC) and execute eligible trades.

    Parameters
    ----------
    signals:
        List of signal dicts sorted by z_score DESC. Must include: ticker, signal_type,
        strategy, strength. z_score is used for priority; strength drives position sizing.
    portfolio:
        Live Portfolio object. Mutated in place for each executed trade.
    current_prices:
        {ticker: float} fill prices for today (in native currency).
    today_indicators:
        {ticker: {indicator_col: value}} from get_todays_indicators().
    sector_map:
        {ticker: sector} pre-fetched from watchlist — executor never queries DB.
    market_map:
        {ticker: market_code} from watchlist (e.g. 'US', 'CA').
    fx_rates:
        {ccy: rate_to_portfolio_currency} e.g. {'USD': 1.37, 'CAD': 1.0} when
        portfolio currency is CAD.

    Returns
    -------
    (executed, skipped)
        executed: list of trade dicts with full execution details.
        skipped:  list of dicts with ticker + reason for skipping.
    """
    max_portfolio_risk   = float(os.environ.get("MAX_PORTFOLIO_RISK", _MAX_PORTFOLIO_RISK))
    atr_multiplier       = float(os.environ.get("ATR_MULTIPLIER", _ATR_MULTIPLIER))
    take_profit_atr_mult = float(os.environ.get("TAKE_PROFIT_ATR_MULT", _TAKE_PROFIT_ATR_MULT))
    min_strength         = float(os.environ.get("MIN_SIGNAL_STRENGTH", "0.10"))
    max_position_size    = float(os.environ.get("MAX_POSITION_SIZE", 0.10))
    max_total_exposure   = float(os.environ.get("MAX_TOTAL_EXPOSURE", 0.80))

    if market_map is None:
        market_map = {}
    if fx_rates is None:
        fx_rates = {}

    # Sort by z_score DESC so statistically extreme signals get first pick of capital.
    # Falls back to strength when z_score is absent (e.g. manually inserted signals).
    signals = sorted(
        signals,
        key=lambda s: float(s.get("z_score") if s.get("z_score") is not None else s.get("strength", 0.0)),
        reverse=True,
    )

    just_opened: set[str] = set()
    executed: list[dict] = []
    skipped:  list[dict] = []

    for signal in signals:
        ticker      = signal.get("ticker", "")
        signal_type = str(signal.get("signal_type", "")).lower()
        strategy    = signal.get("strategy", "unknown")
        strength    = float(signal.get("strength", 0.0))

        if signal_type == "buy":
            # --- Duplicate guard: skip if we already bought this ticker today ---
            if ticker in just_opened:
                skipped.append({"ticker": ticker, "reason": "Already opened this session"})
                continue

            # --- Minimum strength filter ---
            if strength < min_strength:
                skipped.append({
                    "ticker": ticker,
                    "reason": f"Strength {strength:.3f} below minimum {min_strength:.2f}",
                })
                continue

            # --- Price ---
            price = current_prices.get(ticker)
            if price is None:
                skipped.append({"ticker": ticker, "reason": "No fill price available"})
                logger.debug(f"Skipping BUY {ticker}: no fill price")
                continue

            # --- ATR guard ---
            atr = today_indicators.get(ticker, {}).get("atr_14")
            if atr is None or atr == 0:
                skipped.append({"ticker": ticker, "reason": "ATR missing or zero — skipping"})
                logger.warning(f"Skipping BUY {ticker}: ATR missing or zero")
                continue

            # --- Position sizing (in portfolio base currency) ---
            stop_loss       = price - (atr_multiplier * atr)
            risk_per_share  = price - stop_loss
            if risk_per_share <= 0:
                skipped.append({"ticker": ticker, "reason": f"Risk per share <= 0 ({risk_per_share:.4f})"})
                continue

            # Currency conversion: native price → portfolio base currency
            ccy = get_ticker_currency(ticker, market_map)
            fx  = fx_rates.get(ccy, 1.0)
            risk_per_share_base = risk_per_share * fx

            total_value  = portfolio.get_total_value(current_prices, fx_rates, market_map)
            dollar_risk  = strength * max_portfolio_risk * total_value
            qty          = floor(dollar_risk / risk_per_share_base)
            if qty < 1:
                skipped.append({
                    "ticker": ticker,
                    "reason": f"Qty < 1 (dollar_risk=${dollar_risk:.2f}, risk/sh=${risk_per_share_base:.4f})",
                })
                continue

            cost_native = qty * price
            cost_base   = cost_native * fx

            # ------------------------------------------------------------------
            # Cap-and-fill: adjust qty rather than skipping high-conviction trades.
            # ------------------------------------------------------------------
            fill_type = "Normal Fill"

            # 1. Cap to max single-position size.
            max_cost_by_size = max_position_size * total_value
            if cost_base > max_cost_by_size:
                capped_qty = floor(max_cost_by_size / (price * fx))
                if capped_qty < 1:
                    skipped.append({
                        "ticker": ticker,
                        "reason": (
                            f"Position capped to max size {max_position_size:.0%} "
                            f"yields qty < 1 (budget=${max_cost_by_size:.2f})"
                        ),
                    })
                    continue
                qty         = capped_qty
                cost_native = qty * price
                cost_base   = cost_native * fx
                fill_type   = "Capped to Max Size"

            # 2. Partial fill if trade would breach total exposure cap.
            current_exp      = portfolio.get_current_exposure(current_prices, fx_rates, market_map)
            remaining_budget = (max_total_exposure - current_exp) * total_value
            if remaining_budget <= 0:
                skipped.append({
                    "ticker": ticker,
                    "reason": (
                        f"Total exposure {current_exp:.1%} already at or above "
                        f"limit {max_total_exposure:.0%}"
                    ),
                })
                continue
            if cost_base > remaining_budget:
                partial_qty = floor(remaining_budget / (price * fx))
                if partial_qty < 1:
                    skipped.append({
                        "ticker": ticker,
                        "reason": (
                            f"Partial fill for exposure cap yields qty < 1 "
                            f"(budget=${remaining_budget:.2f})"
                        ),
                    })
                    continue
                qty         = partial_qty
                cost_native = qty * price
                cost_base   = cost_native * fx
                fill_type   = "Capped & Partial" if fill_type == "Capped to Max Size" else "Partial Fill"
            # ------------------------------------------------------------------

            take_profit = price + (take_profit_atr_mult * atr)
            sector      = sector_map.get(ticker, "Unknown")

            allowed, reason = portfolio.can_open_position(ticker, cost_base, sector, current_prices, fx_rates, market_map)
            if not allowed:
                skipped.append({"ticker": ticker, "reason": reason})
                logger.debug(f"Skipping BUY {ticker}: {reason}")
                continue

            portfolio.open_position(ticker, qty, price, stop_loss, take_profit, strategy, sector, ccy, fx)
            just_opened.add(ticker)
            trade = {
                "ticker":          ticker,
                "side":            "buy",
                "quantity":        qty,
                "fill_price":      round(price, 2),
                "stop_loss":       round(stop_loss, 2),
                "take_profit":     round(take_profit, 2),
                "strategy":        strategy,
                "signal_strength": strength,
                "reason":          signal.get("reason", ""),
                "signal_data":     signal.get("indicator_vals"),
                "signal_id":       signal.get("id"),
                "currency":        ccy,
                "fx_rate":         round(fx, 6),
                "fill_type":       fill_type,
            }
            executed.append(trade)
            logger.info(
                f"BUY  {ticker}: {qty}sh @ ${price:.2f} "
                f"SL=${stop_loss:.2f} TP=${take_profit:.2f} [{strategy}] "
                f"str={strength:.2f} [{fill_type}]"
            )

        elif signal_type == "sell":
            if ticker not in portfolio.positions:
                skipped.append({"ticker": ticker, "reason": "No open position to sell"})
                continue

            if ticker in just_opened:
                skipped.append({
                    "ticker": ticker,
                    "reason": f"Position opened today — minimum 1 day hold",
                })
                logger.info(f"Skipping SELL {ticker} — position opened today, minimum 1 day hold")
                continue

            fill_price = current_prices.get(ticker, portfolio.positions[ticker]["avg_cost"])
            original_strategy = portfolio.positions[ticker].get("strategy", strategy)
            original_qty      = portfolio.positions[ticker]["qty"]
            sell_ccy  = portfolio.positions[ticker].get("currency", "USD")
            sell_fx   = fx_rates.get(sell_ccy, portfolio.positions[ticker].get("fx_rate", 1.0))
            pnl = portfolio.close_position(ticker, fill_price)
            trade = {
                "ticker":          ticker,
                "side":            "sell",
                "quantity":        original_qty,
                "fill_price":      round(fill_price, 2),
                "stop_loss":       None,
                "take_profit":     None,
                "strategy":        original_strategy,
                "signal_strength": strength,
                "reason":          signal.get("reason", "Sell signal"),
                "signal_data":     signal.get("indicator_vals"),
                "signal_id":       signal.get("id"),
                "pnl":             round(pnl, 2),
                "currency":        sell_ccy,
                "fx_rate":         round(sell_fx, 6),
            }
            executed.append(trade)
            logger.info(
                f"SELL {ticker}: {original_qty}sh @ ${fill_price:.2f} "
                f"P&L=${pnl:.2f} [{original_strategy}]"
            )

        else:
            # hold or unrecognised — skip silently
            continue

    return executed, skipped
