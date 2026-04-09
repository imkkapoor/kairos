"""
backtesting/portfolio_runner.py — Day-by-day portfolio simulation engine.

Replicates Phase 3 simulator logic exactly:
  - ATR-based position sizing (same formula as executor.py)
  - Stop-loss / take-profit at market Open (same as run_morning)
  - Z-score priority sorting (population std, ±3.0 clamp)
  - Regime detection via strategies/regime.py (detect_all)
  - All five strategy functions called directly from strategies/

All P&L stored in USD. CAD stocks converted using the daily CADUSD rate
loaded from fx_rates table.

Rules:
  - No DB access — data passed in as pre-loaded DataFrames.
  - No yfinance calls.
  - Fill price is always the day's Open (no look-ahead bias).
  - Close price used only for end-of-day portfolio valuation.
"""

import os
from collections import defaultdict
from datetime import date
from math import floor

import numpy as np
import pandas as pd

import strategies.rsi as rsi_strategy
import strategies.momentum as momentum_strategy
import strategies.macd as macd_strategy
import strategies.reversal as reversal_strategy
import strategies.sector_rotation as sector_rotation_strategy
from strategies.regime import detect_all
from strategies.vol_regime import get_vix_regime
from backtesting.config import (
    INITIAL_CAPITAL,
    MAX_PORTFOLIO_RISK,
    ATR_MULTIPLIER,
    TAKE_PROFIT_ATR_MULT,
    MAX_POSITION_SIZE,
    MAX_TOTAL_EXPOSURE,
)

_Z_SCORE_CAP = 3.0


# ---------------------------------------------------------------------------
# Z-score helper (mirrors scanner.py exactly)
# ---------------------------------------------------------------------------

def _compute_z_scores(signals: list[dict]) -> None:
    """Compute cross-universe z-scores in place, grouped by strategy.

    Population std, clamped to ±3.0. Sets z_score=0.0 when std=0.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        groups[sig.get("strategy", "unknown")].append(sig)

    for strategy, sigs in groups.items():
        strengths = [float(s["strength"]) for s in sigs]
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
                raw = (x - mean) / std
                s["z_score"] = max(min(raw, _Z_SCORE_CAP), -_Z_SCORE_CAP)


# ---------------------------------------------------------------------------
# BacktestPortfolio
# ---------------------------------------------------------------------------

class BacktestPortfolio:
    """In-memory paper portfolio for backtesting. All values in USD.

    Positions dict structure per ticker:
        {qty, avg_cost_usd, stop_loss, take_profit, strategy, sector, is_cad}

    avg_cost_usd: cost per share expressed in USD at the time of opening.
    stop_loss / take_profit are stored in the ticker's native currency
    (same as Phase 3 executor — comparison happens against native Open price).
    """

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        max_portfolio_risk: float = MAX_PORTFOLIO_RISK,
        atr_multiplier: float = ATR_MULTIPLIER,
        take_profit_atr_mult: float = TAKE_PROFIT_ATR_MULT,
        max_position_size: float = MAX_POSITION_SIZE,
        max_total_exposure: float = MAX_TOTAL_EXPOSURE,
    ) -> None:
        self.cash = initial_capital          # USD
        self.initial_capital = initial_capital
        self.positions: dict = {}            # {ticker: position_dict}
        self.peak_value = initial_capital

        # Risk params
        self.max_portfolio_risk   = max_portfolio_risk
        self.atr_multiplier       = atr_multiplier
        self.take_profit_atr_mult = take_profit_atr_mult
        self.max_position_size    = max_position_size
        self.max_total_exposure   = max_total_exposure

    # ------------------------------------------------------------------
    # Core position operations (USD accounting)
    # ------------------------------------------------------------------

    def open_position(
        self,
        ticker: str,
        qty: int,
        price: float,          # native currency
        stop_loss: float,      # native currency
        take_profit: float,    # native currency
        strategy: str,
        sector: str,
        fx_rate: float,        # native → USD (1.0 for US stocks)
    ) -> None:
        is_cad = ticker.endswith(".TO")
        cost_native = qty * price
        cost_usd    = cost_native * fx_rate if is_cad else cost_native
        avg_cost_usd = price * fx_rate if is_cad else price  # per share in USD
        self.cash  -= cost_usd
        self.positions[ticker] = {
            "qty":          qty,
            "avg_cost_usd": avg_cost_usd,
            "stop_loss":    stop_loss,
            "take_profit":  take_profit,
            "strategy":     strategy,
            "sector":       sector,
            "is_cad":       is_cad,
        }

    def close_position(
        self,
        ticker: str,
        exit_price: float,  # native currency
        fx_rate: float,
    ) -> float:
        """Close a position, credit cash, and return P&L in USD."""
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return 0.0
        qty = pos["qty"]
        proceeds_native = qty * exit_price
        proceeds_usd    = proceeds_native * fx_rate if pos["is_cad"] else proceeds_native
        pnl_usd         = proceeds_usd - (qty * pos["avg_cost_usd"])
        self.cash += proceeds_usd
        return pnl_usd

    def get_total_value(self, current_prices: dict, fx_rate: float) -> float:
        """Return total portfolio value in USD (cash + mark-to-market positions)."""
        market_usd = 0.0
        for ticker, pos in self.positions.items():
            price = current_prices.get(ticker, 0.0)
            val   = pos["qty"] * price
            market_usd += val * fx_rate if pos["is_cad"] else val
        return self.cash + market_usd

    # ------------------------------------------------------------------
    # Risk checks (mirrors executor.py / portfolio.py logic)
    # ------------------------------------------------------------------

    def can_open(
        self,
        ticker: str,
        cost_usd: float,
        sector: str,
        close_prices: dict,
        fx_rate: float,
        max_open_positions: int,
        max_sector_exposure: float,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). cost_usd must be in USD."""
        if len(self.positions) >= max_open_positions:
            return False, "max_open_positions"

        total = self.get_total_value(close_prices, fx_rate)
        if total == 0:
            return False, "zero_portfolio_value"

        # Total exposure cap
        invested = sum(
            pos["qty"] * close_prices.get(t, 0.0) * (fx_rate if pos["is_cad"] else 1.0)
            for t, pos in self.positions.items()
        )
        if (invested + cost_usd) / total > self.max_total_exposure:
            return False, "total_exposure"

        # Sector cap
        sector_val = sum(
            pos["qty"] * close_prices.get(t, 0.0) * (fx_rate if pos["is_cad"] else 1.0)
            for t, pos in self.positions.items()
            if pos.get("sector") == sector
        )
        if (sector_val + cost_usd) / total > max_sector_exposure:
            return False, "sector_cap"

        # Cash check
        if cost_usd > self.cash:
            return False, "insufficient_cash"

        return True, "ok"


# ---------------------------------------------------------------------------
# Per-window simulation
# ---------------------------------------------------------------------------

def _build_day_indicators(
    day_ts: pd.Timestamp,
    indicators: dict[str, pd.DataFrame],
    ohlcv: dict[str, pd.DataFrame],
) -> dict[str, dict]:
    """Build the today_indicators dict for a single trading day.

    Merges indicator columns with Close and Volume from OHLCV so that
    strategy functions receive the same complete row_dict they get in live runs.
    """
    today_ind: dict[str, dict] = {}
    for ticker, ind_df in indicators.items():
        if day_ts not in ind_df.index:
            continue
        row = ind_df.loc[day_ts].to_dict()
        # Attach close and volume from OHLCV (lowercase to match strategy expectations)
        ohlcv_df = ohlcv.get(ticker)
        if ohlcv_df is not None and day_ts in ohlcv_df.index:
            row["close"]  = float(ohlcv_df.at[day_ts, "Close"])
            row["volume"] = float(ohlcv_df.at[day_ts, "Volume"])
        today_ind[ticker] = row
    return today_ind


def _build_prev_indicators(
    day_ts: pd.Timestamp,
    indicators: dict[str, pd.DataFrame],
    ohlcv: dict[str, pd.DataFrame],
) -> dict[str, dict]:
    """Return indicator rows for the most recent trading day before day_ts."""
    prev_ind: dict[str, dict] = {}
    for ticker, ind_df in indicators.items():
        earlier = ind_df[ind_df.index < day_ts]
        if earlier.empty:
            continue
        row = earlier.iloc[-1].to_dict()
        ohlcv_df = ohlcv.get(ticker)
        if ohlcv_df is not None:
            prev_ts = earlier.index[-1]
            if prev_ts in ohlcv_df.index:
                row["close"]  = float(ohlcv_df.at[prev_ts, "Close"])
                row["volume"] = float(ohlcv_df.at[prev_ts, "Volume"])
        prev_ind[ticker] = row
    return prev_ind


def run_window(
    test_start: date,
    test_end: date,
    config: dict,
    ohlcv: dict[str, pd.DataFrame],
    indicators: dict[str, pd.DataFrame],
    fx_rates: "pd.Series | None",
    sector_map: dict[str, str],
    vix_series: "pd.Series | None" = None,
) -> dict:
    """Simulate one WFA test window and return a metrics dict.

    Parameters
    ----------
    test_start / test_end:
        Inclusive test date range.
    config:
        Allocation config dict with strategy_weights, regime_overrides,
        min_strength, max_open_positions, max_sector_exposure.
        Optional 'use_vol_filter' key (bool) enables Phase 4.5 VIX regime logic.
    ohlcv:
        {ticker: DataFrame(Open, High, Low, Close, Volume)} UTC index.
    indicators:
        {ticker: DataFrame(indicator columns)} UTC index.
    fx_rates:
        pd.Series indexed by UTC Timestamp → rate (native → USD).
        None for USD-only portfolios.
    sector_map:
        {ticker: sector} from watchlist.
    vix_series:
        pd.Series indexed by UTC Timestamps → VIX close.  Required when
        use_vol_filter=True; ignored when False.  Pass None to disable.
    """
    portfolio = BacktestPortfolio()
    trade_log: list[dict] = []   # {ticker, pnl_usd, pct, exit_type, cost_usd}
    equity_curve: list[tuple[date, float]] = []

    max_open_positions  = config.get("max_open_positions",  20)
    max_sector_exposure = config.get("max_sector_exposure", 0.30)
    min_strength        = config.get("min_strength",        0.10)

    # Phase 4.5: vol filter state
    use_vol = config.get("use_vol_filter", False)
    vroc_window    = config.get("vroc_window",    10)
    vroc_threshold = config.get("vroc_threshold", 0.20)
    vix_day_values: list[float] = []
    elevated_day_count: int = 0
    spike_day_count: int = 0

    # Phase 4.7: circuit breaker state
    use_circuit_breaker = config.get("use_circuit_breaker", False)
    dd_trigger = config.get("dd_trigger", 0.15)
    dd_reset   = config.get("dd_reset",   0.10)
    cb_peak_value = INITIAL_CAPITAL
    circuit_breaker_active = False
    breaker_active_day_count: int = 0
    circuit_breaker_days: list[bool] = []

    # Build sorted list of trading days within test window using SPY as calendar
    ref_ticker = "SPY" if "SPY" in ohlcv else next(iter(ohlcv), None)
    if ref_ticker is None:
        return _empty_metrics(test_start, test_end)

    test_start_ts = pd.Timestamp(test_start, tz="UTC")
    test_end_ts   = pd.Timestamp(test_end, tz="UTC")
    ref_df        = ohlcv[ref_ticker]
    trading_days  = ref_df.index[
        (ref_df.index >= test_start_ts) & (ref_df.index < test_end_ts)
    ].tolist()

    if not trading_days:
        return _empty_metrics(test_start, test_end)

    all_tickers = list(ohlcv.keys())

    for day_ts in trading_days:
        day_date = day_ts.date()

        # ── 1. Prices for today ──────────────────────────────────────────
        open_prices:  dict[str, float] = {}
        close_prices: dict[str, float] = {}
        for ticker, df in ohlcv.items():
            if day_ts in df.index:
                open_prices[ticker]  = float(df.at[day_ts, "Open"])
                close_prices[ticker] = float(df.at[day_ts, "Close"])

        # ── 2. FX rate for today ─────────────────────────────────────────
        if fx_rates is not None:
            fx_today = float(fx_rates.get(day_ts, fx_rates.iloc[-1]))
        else:
            fx_today = 1.0

        # ── 3. Indicator rows ─────────────────────────────────────────────
        today_ind = _build_day_indicators(day_ts, indicators, ohlcv)
        prev_ind  = _build_prev_indicators(day_ts, indicators, ohlcv)

        # ── 4. SPY crisis check (last 10 bars before today) ──────────────
        spy_ohlcv = ohlcv.get("SPY")
        spy_crisis_df = None
        if spy_ohlcv is not None:
            spy_hist = spy_ohlcv[spy_ohlcv.index <= day_ts].tail(10)
            if not spy_hist.empty:
                spy_crisis_df = spy_hist[["Close"]].rename(columns={"Close": "close"})

        # ── 5. Regime detection ───────────────────────────────────────────
        regimes = detect_all(today_ind, spy_crisis_df)

        # ── 6. ROC rankings for reversal strategy ─────────────────────────
        all_roc = {
            t: float(row["roc_20"])
            for t, row in today_ind.items()
            if row.get("roc_20") is not None
        }
        sorted_by_roc = sorted(all_roc, key=lambda t: all_roc[t])
        roc_rankings  = (
            {t: i / len(sorted_by_roc) for i, t in enumerate(sorted_by_roc)}
            if sorted_by_roc else {}
        )

        # ── 7. Sector scores for sector rotation ─────────────────────────
        _etfs = ["XLE", "XLK", "TLT", "XLU", "XLV", "SPY"]
        if all(
            e in today_ind and today_ind[e].get("roc_20") is not None
            for e in _etfs
        ):
            xle = float(today_ind["XLE"]["roc_20"])
            xlk = float(today_ind["XLK"]["roc_20"])
            tlt = float(today_ind["TLT"]["roc_20"])
            xlu = float(today_ind["XLU"]["roc_20"])
            xlv = float(today_ind["XLV"]["roc_20"])
            spy = float(today_ind["SPY"]["roc_20"])
            sector_scores: dict | None = {
                "late_cycle": (xle - xlk) + (-tlt * 0.5),
                "defensive":  ((xlu - spy) + (xlv - spy)) / 2,
            }
        else:
            sector_scores = None

        # ── 8. Stop-loss / take-profit exits (at Open) ───────────────────
        to_exit: list[tuple[str, float, str]] = []
        for ticker, pos in list(portfolio.positions.items()):
            op = open_prices.get(ticker)
            if op is None:
                continue
            if op <= pos["stop_loss"]:
                to_exit.append((ticker, op, "stop_loss"))
            elif op >= pos["take_profit"]:
                to_exit.append((ticker, op, "take_profit"))

        for ticker, exit_px, exit_type in to_exit:
            cost_usd = portfolio.positions[ticker]["qty"] * portfolio.positions[ticker]["avg_cost_usd"]
            pnl = portfolio.close_position(ticker, exit_px, fx_today)
            pnl_pct = pnl / cost_usd if cost_usd > 0 else 0.0
            trade_log.append({"ticker": ticker, "pnl_usd": pnl, "pnl_pct": pnl_pct, "exit_type": exit_type})

        # ── 8a. Circuit breaker state update (after exits, before new buys) ─
        if use_circuit_breaker:
            cb_val = portfolio.get_total_value(open_prices, fx_today)
            current_dd = (cb_peak_value - cb_val) / cb_peak_value if cb_peak_value > 0 else 0.0
            cb_peak_value = max(cb_peak_value, cb_val)
            if current_dd >= dd_trigger:
                circuit_breaker_active = True
            elif current_dd < dd_reset:
                circuit_breaker_active = False
            if circuit_breaker_active:
                breaker_active_day_count += 1
        circuit_breaker_days.append(circuit_breaker_active)

        # ── 9. Open position tickers (after exits) ────────────────────────
        open_tickers = list(portfolio.positions.keys())

        # ── 9a. Vol filter state for today ────────────────────────────────
        if use_vol and vix_series is not None and not vix_series.empty:
            vol_state = get_vix_regime(
                day_date, vix_series,
                sma_window=vroc_window,
                vroc_threshold=vroc_threshold,
            )
            size_mult = vol_state["size_mult"]
            suppressed_strategies = vol_state["suppressed"]
            if vol_state["vix"] is not None:
                vix_day_values.append(vol_state["vix"])
            if vol_state["regime"] in ("HIGH", "EXTREME"):
                elevated_day_count += 1
            if vol_state.get("spike_triggered"):
                spike_day_count += 1
        else:
            size_mult = 1.0
            suppressed_strategies: set[str] = set()

        # ── 10. Generate signals from all strategies ──────────────────────
        all_signals: list[dict] = []
        all_signals.extend(rsi_strategy.generate_signals(
            all_tickers, today_ind, regimes, open_tickers))
        all_signals.extend(momentum_strategy.generate_signals(
            all_tickers, today_ind, prev_ind, regimes, open_tickers))
        all_signals.extend(macd_strategy.generate_signals(
            all_tickers, today_ind, prev_ind, regimes, open_tickers))
        all_signals.extend(reversal_strategy.generate_signals(
            all_tickers, today_ind, regimes, open_tickers, roc_rankings))
        all_signals.extend(sector_rotation_strategy.generate_signals(
            all_tickers, today_ind, regimes, open_tickers, sector_scores, sector_map))

        # Keep only BUY signals (SELL exits handled by stop/TP above)
        buy_signals = [s for s in all_signals if str(s.get("signal_type", "")).upper() == "BUY"]

        # ── 11. Apply config weights + regime overrides ───────────────────
        for sig in buy_signals:
            strategy_name = sig.get("strategy", "")
            tkr           = sig.get("ticker", "")
            regime_lower  = regimes.get(tkr, {}).get("regime", "CHOPPY").lower()

            w  = config["strategy_weights"].get(strategy_name, 1.0)
            rw = (
                config.get("regime_overrides", {})
                .get(regime_lower, {})
                .get(strategy_name, 1.0)
            )
            sig["strength"] = min(sig["strength"] * w * rw, 1.0)

        # ── 11a. Apply vol suppression (zeroes strength → drops at min_strength) ─
        if suppressed_strategies:
            for sig in buy_signals:
                if sig.get("strategy") in suppressed_strategies:
                    sig["strength"] = 0.0

        # ── 12. Filter by min strength ────────────────────────────────────
        buy_signals = [s for s in buy_signals if s["strength"] >= min_strength]

        # ── 13. Z-scores + sort ───────────────────────────────────────────
        _compute_z_scores(buy_signals)
        buy_signals.sort(
            key=lambda s: (s.get("z_score", 0.0), s["strength"]),
            reverse=True,
        )

        # ── 13a. Circuit breaker: suppress all BUY signals ───────────────
        if use_circuit_breaker and circuit_breaker_active:
            buy_signals = []

        # ── 14. Execute trades ────────────────────────────────────────────
        just_opened: set[str] = set()
        for sig in buy_signals:
            tkr      = sig["ticker"]
            strength = float(sig["strength"])
            strategy_name = sig.get("strategy", "unknown")

            if tkr in just_opened or tkr in portfolio.positions:
                continue

            fill_price = open_prices.get(tkr)
            if fill_price is None or fill_price <= 0:
                continue

            atr = today_ind.get(tkr, {}).get("atr_14")
            if atr is None:
                continue
            try:
                atr = float(atr)
            except (TypeError, ValueError):
                continue
            if atr == 0:
                continue

            stop_loss    = fill_price - portfolio.atr_multiplier * atr
            take_profit  = fill_price + portfolio.take_profit_atr_mult * atr
            risk_per_native = fill_price - stop_loss
            if risk_per_native <= 0:
                continue

            # USD→native conversion
            is_cad = tkr.endswith(".TO")
            risk_per_usd = risk_per_native * fx_today if is_cad else risk_per_native

            total_val  = portfolio.get_total_value(close_prices, fx_today)
            dollar_risk = strength * portfolio.max_portfolio_risk * total_val * size_mult
            qty = floor(dollar_risk / risk_per_usd)
            if qty < 1:
                continue

            cost_native = qty * fill_price
            cost_usd    = cost_native * fx_today if is_cad else cost_native

            # Cap to max single position size
            max_by_size = portfolio.max_position_size * total_val
            if cost_usd > max_by_size:
                qty = floor(max_by_size / (fill_price * fx_today if is_cad else fill_price))
                if qty < 1:
                    continue
                cost_native = qty * fill_price
                cost_usd    = cost_native * fx_today if is_cad else cost_native

            # Partial fill if exposure cap reached
            invested = sum(
                pos["qty"] * close_prices.get(t, 0.0) * (fx_today if pos["is_cad"] else 1.0)
                for t, pos in portfolio.positions.items()
            )
            remaining = (portfolio.max_total_exposure - invested / total_val) * total_val
            if remaining <= 0:
                continue
            if cost_usd > remaining:
                qty = floor(remaining / (fill_price * fx_today if is_cad else fill_price))
                if qty < 1:
                    continue
                cost_native = qty * fill_price
                cost_usd    = cost_native * fx_today if is_cad else cost_native

            sector = sector_map.get(tkr, "Unknown")
            allowed, _ = portfolio.can_open(
                tkr, cost_usd, sector, close_prices, fx_today,
                max_open_positions, max_sector_exposure,
            )
            if not allowed:
                continue

            portfolio.open_position(
                tkr, qty, fill_price, stop_loss, take_profit,
                strategy_name, sector, fx_today,
            )
            just_opened.add(tkr)

        # ── 15. Record equity curve ───────────────────────────────────────
        total_val = portfolio.get_total_value(close_prices, fx_today)
        if total_val > portfolio.peak_value:
            portfolio.peak_value = total_val
        equity_curve.append((day_date, total_val))

    # ── Force-close any remaining positions at last Close ─────────────────
    if equity_curve:
        last_day_ts = trading_days[-1]
        last_close  = {t: float(df.at[last_day_ts, "Close"])
                       for t, df in ohlcv.items() if last_day_ts in df.index}
        final_fx = float(fx_rates.iloc[-1]) if fx_rates is not None else 1.0
        for ticker in list(portfolio.positions.keys()):
            ep = last_close.get(ticker, 0.0)
            if ep > 0:
                cost_usd = portfolio.positions[ticker]["qty"] * portfolio.positions[ticker]["avg_cost_usd"]
                pnl = portfolio.close_position(ticker, ep, final_fx)
                pnl_pct = pnl / cost_usd if cost_usd > 0 else 0.0
                trade_log.append({"ticker": ticker, "pnl_usd": pnl, "pnl_pct": pnl_pct, "exit_type": "end_of_window"})

    metrics = _compute_metrics(trade_log, equity_curve, test_start, test_end, portfolio.initial_capital)

    # Phase 4.5 / 4.6: vol filter summary metrics
    metrics["use_vol_filter"] = use_vol
    n_days = len(trading_days)
    if use_vol and vix_series is not None and not vix_series.empty:
        metrics["avg_vix"]           = float(np.mean(vix_day_values)) if vix_day_values else None
        metrics["pct_days_elevated"] = elevated_day_count / n_days if n_days > 0 else None
        metrics["pct_days_spike"]    = spike_day_count / n_days if n_days > 0 else None
        metrics["vroc_window"]       = vroc_window
        metrics["vroc_threshold"]    = vroc_threshold
    else:
        metrics["avg_vix"]           = None
        metrics["pct_days_elevated"] = None
        metrics["pct_days_spike"]    = None
        metrics["vroc_window"]       = None
        metrics["vroc_threshold"]    = None

    # Phase 4.7: circuit breaker metrics
    metrics["use_circuit_breaker"]     = use_circuit_breaker
    metrics["dd_trigger"]              = dd_trigger if use_circuit_breaker else None
    metrics["dd_reset"]                = dd_reset if use_circuit_breaker else None
    metrics["pct_days_breaker_active"] = (
        breaker_active_day_count / n_days if (use_circuit_breaker and n_days > 0) else None
    )
    metrics["circuit_breaker_days"]    = circuit_breaker_days  # per-day list, not persisted to DB

    return metrics


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def _empty_metrics(test_start: date, test_end: date) -> dict:
    return {
        "window_start": test_start,
        "window_end":   test_end,
        "total_trades": 0,
        "win_rate":     None,
        "avg_win_pct":  None,
        "avg_loss_pct": None,
        "profit_factor": None,
        "cagr":         None,
        "sharpe_ratio": None,
        "calmar_ratio": None,
        "max_drawdown": None,
        "final_value_usd": INITIAL_CAPITAL,
        "total_pnl_usd":   0.0,
        "annualized_vol":  None,
        "equity_curve":    [],
        # Phase 4.5 / 4.6 vol filter fields
        "use_vol_filter":    False,
        "avg_vix":           None,
        "pct_days_elevated": None,
        "pct_days_spike":    None,
        "vroc_window":       None,
        "vroc_threshold":    None,
        # Phase 4.7 circuit breaker fields
        "use_circuit_breaker":     False,
        "dd_trigger":              None,
        "dd_reset":                None,
        "pct_days_breaker_active": None,
        "circuit_breaker_days":    [],
    }


def _compute_metrics(
    trade_log: list[dict],
    equity_curve: list[tuple[date, float]],
    test_start: date,
    test_end: date,
    initial_capital: float,
) -> dict:
    """Compute all performance metrics from trade_log and equity_curve."""
    result: dict = {
        "window_start": test_start,
        "window_end":   test_end,
        "equity_curve": equity_curve,
    }

    # Trade stats
    closed = [t for t in trade_log if t.get("exit_type") != "end_of_window"]
    total_trades = len(closed)
    result["total_trades"] = total_trades

    if total_trades > 0:
        wins   = [t for t in closed if t["pnl_usd"] > 0]
        losses = [t for t in closed if t["pnl_usd"] <= 0]
        result["win_rate"]    = len(wins) / total_trades
        result["avg_win_pct"] = (
            float(np.mean([t["pnl_pct"] for t in wins])) if wins else 0.0
        )
        result["avg_loss_pct"] = (
            float(np.mean([t["pnl_pct"] for t in losses])) if losses else 0.0
        )
        total_win_usd  = sum(t["pnl_usd"] for t in wins)
        total_loss_usd = abs(sum(t["pnl_usd"] for t in losses))
        result["profit_factor"] = (
            total_win_usd / total_loss_usd if total_loss_usd > 0 else None
        )
    else:
        result["win_rate"] = result["avg_win_pct"] = result["avg_loss_pct"] = None
        result["profit_factor"] = None

    # Equity curve metrics
    if len(equity_curve) < 2:
        result.update({
            "cagr": None, "sharpe_ratio": None, "calmar_ratio": None,
            "max_drawdown": None, "annualized_vol": None,
        })
        final_val = equity_curve[-1][1] if equity_curve else initial_capital
        result["final_value_usd"] = final_val
        result["total_pnl_usd"]   = final_val - initial_capital
        return result

    values = np.array([v for _, v in equity_curve], dtype=float)
    final_val = float(values[-1])
    result["final_value_usd"] = final_val
    result["total_pnl_usd"]   = final_val - initial_capital

    # Daily returns
    daily_returns = np.diff(values) / values[:-1]
    annualized_vol = float(np.std(daily_returns, ddof=0) * np.sqrt(252))
    result["annualized_vol"] = annualized_vol

    # CAGR
    days = max((test_end - test_start).days, 1)
    cagr = (final_val / initial_capital) ** (365.0 / days) - 1
    result["cagr"] = float(cagr)

    # Sharpe (risk-free rate = 0 for simplicity)
    ann_return = float(np.mean(daily_returns) * 252)
    result["sharpe_ratio"] = float(ann_return / annualized_vol) if annualized_vol > 0 else None

    # Max drawdown
    peak    = np.maximum.accumulate(values)
    dd      = (peak - values) / peak
    max_dd  = float(np.max(dd))
    result["max_drawdown"] = max_dd

    # Calmar
    result["calmar_ratio"] = float(cagr / max_dd) if max_dd > 0 else None

    return result


# ---------------------------------------------------------------------------
# WFA orchestrator
# ---------------------------------------------------------------------------

def run_wfa(
    config: dict,
    config_name: str,
    windows: list[dict],
    tickers: list[str],
    sector_map: dict[str, str],
    progress_prefix: str = "",
) -> list[dict]:
    """Run all WFA windows for one allocation config.

    Data for the full date range is loaded once (not per window) so overlapping
    windows share the same in-memory DataFrames. Each window gets a slice.

    Returns list of per-window result dicts.
    """
    from backtesting.data_loader import load_ohlcv, load_indicators, load_fx_rates

    if not windows:
        return []

    # Load full date range in one shot to avoid redundant DB queries
    all_start = windows[0]["train_start"]
    all_end   = windows[-1]["test_end"]

    try:
        from rich.console import Console
        from rich.progress import (
            Progress, SpinnerColumn, TextColumn, BarColumn,
            MofNCompleteColumn, TimeElapsedColumn,
        )
        _rich = True
    except ImportError:
        _rich = False

    if _rich:
        out = Console()
        out.print(f"  [cyan]Loading data ({all_start} → {all_end})…[/cyan]")

    ohlcv_all      = load_ohlcv(tickers, all_start, all_end)
    indicators_all = load_indicators(tickers, all_start, all_end)
    fx_rates_all   = load_fx_rates(all_start, all_end)

    # Phase 4.5: load VIX once for the full range if this config uses vol filter
    use_vol = config.get("use_vol_filter", False)
    if use_vol:
        from backtesting.data_loader import load_vix
        vix_all = load_vix(all_start, all_end)
    else:
        vix_all = None

    results: list[dict] = []
    n_windows = len(windows)

    _progress_ctx = (
        Progress(
            SpinnerColumn(),
            TextColumn(f"  {progress_prefix}[bold]{{task.description}}[/bold]"),
            BarColumn(bar_width=28),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        if _rich else None
    )
    _task_id = _progress_ctx.add_task(config_name, total=n_windows) if _progress_ctx else None
    if _progress_ctx:
        _progress_ctx.start()

    for wnd in windows:
        win_idx    = wnd["window_index"]
        test_start = wnd["test_start"]
        test_end   = wnd["test_end"]

        if _progress_ctx:
            _progress_ctx.update(
                _task_id,
                description=f"{config_name}  {test_start} → {test_end}",
            )

        # Slice to test window (keep a 10-day buffer before test_start for prev_indicators)
        buf_start = pd.Timestamp(test_start, tz="UTC") - pd.Timedelta(days=20)

        ohlcv_win = {
            t: df[df.index >= buf_start]
            for t, df in ohlcv_all.items()
            if not df.empty
        }
        ind_win = {
            t: df[df.index >= buf_start]
            for t, df in indicators_all.items()
            if not df.empty
        }
        fx_win = (
            fx_rates_all[fx_rates_all.index >= buf_start]
            if fx_rates_all is not None else None
        )
        vix_win = (
            vix_all[vix_all.index >= buf_start]
            if vix_all is not None and not vix_all.empty else None
        )

        metrics = run_window(
            test_start, test_end, config,
            ohlcv_win, ind_win, fx_win, sector_map,
            vix_series=vix_win,
        )
        metrics["config_name"]  = config_name
        metrics["config"]       = config
        metrics["window_index"] = win_idx
        results.append(metrics)

        if _progress_ctx:
            _progress_ctx.advance(_task_id)

    if _progress_ctx:
        _progress_ctx.stop()

    return results


def run_all_configs(
    configs: dict[str, dict],
    windows: list[dict],
    tickers: list[str],
    sector_map: dict[str, str],
) -> "pd.DataFrame":
    """Run all allocation configs across all WFA windows, store to DB.

    Returns a summary DataFrame.
    """
    from db.connection import insert_backtest_result, get_backtest_summary
    import os
    import uuid

    currency = os.environ.get("PORTFOLIO_CURRENCY", "CAD")
    n_configs = len(configs)

    try:
        from rich.console import Console
        _con = Console()
    except ImportError:
        _con = None

    for idx, (config_name, config) in enumerate(configs.items(), 1):
        if _con:
            _con.rule(f"[bold green]Config {idx}/{n_configs}: {config_name}[/bold green]")

        window_results = run_wfa(
            config, config_name, windows, tickers, sector_map,
            progress_prefix=f"[{idx}/{n_configs}] ",
        )

        # Persist each window result — each config gets its own run_id
        run_id = str(uuid.uuid4())
        for res in window_results:
            db_row = {k: v for k, v in res.items() if k != "equity_curve"}
            db_row["currency"] = currency
            db_row["run_id"]   = run_id
            insert_backtest_result(db_row)

    return get_backtest_summary()
