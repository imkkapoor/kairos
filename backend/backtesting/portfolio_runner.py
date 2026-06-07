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
        {qty, avg_cost_usd, stop_loss, take_profit, strategy, sector, is_cad, opened_date}

    avg_cost_usd: cost per share expressed in USD at the time of opening.
    stop_loss / take_profit are stored in the ticker's native currency
    (same as Phase 3 executor — comparison happens against native Open price).
    opened_date: date object for time-based exit tracking.
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
        opened_date=None,      # date object for time-based exit tracking
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
            "opened_date":  opened_date,
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
    initial_capital: float | None = None,
) -> dict:
    """Simulate one ROOS test window and return a metrics dict.

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
    initial_capital:
        Starting capital for this window. Defaults to INITIAL_CAPITAL when None.
        Used by capital_compounded mode to carry forward capital.
    """
    _capital = initial_capital if initial_capital is not None else INITIAL_CAPITAL
    portfolio = BacktestPortfolio(initial_capital=_capital)
    trade_log: list[dict] = []   # {ticker, pnl_usd, pct, exit_type, cost_usd}
    equity_curve: list[tuple[date, float]] = []
    daily_snapshots: list[dict] = []  # {date, positions, cash, total_value, total_pnl}

    max_open_positions  = config.get("max_open_positions",  20)
    max_sector_exposure = config.get("max_sector_exposure", 0.30)
    min_strength        = config.get("min_strength",        0.10)

    # Phase 4.5: vol filter state
    use_vol = config.get("use_vol_filter", False)
    vroc_window    = config.get("vroc_window",    10)
    vroc_threshold = config.get("vroc_threshold", 0.50)
    vix_day_values: list[float] = []
    elevated_day_count: int = 0
    spike_day_count: int = 0

    # Phase 4.7: legacy binary circuit breaker state (backward compat)
    use_circuit_breaker = config.get("use_circuit_breaker", False)
    dd_trigger = config.get("dd_trigger", 0.15)
    dd_reset   = config.get("dd_reset",   0.10)
    cb_peak_value = _capital
    circuit_breaker_active = False
    breaker_active_day_count: int = 0
    circuit_breaker_days: list[bool] = []

    # Phase 4.8: soft circuit breaker — linear de-leveraging (Change 1 + 2)
    # use_soft_cb wins if both flags are set
    use_soft_cb    = config.get("use_soft_cb", False)
    cb_soft_start  = config.get("cb_soft_start", 0.05)
    cb_hard_stop   = config.get("cb_hard_stop",  0.12)
    cb_min_mult    = config.get("cb_min_mult",   0.25)
    if use_soft_cb:
        use_circuit_breaker = False
    soft_cb_peak_value:  float = _capital
    soft_cb_was_active:  bool  = False
    soft_peak_dd:        float = 0.0
    days_since_peak_dd:  int   = 0
    last_cb_mult:        float = 1.0
    cb_mult_values:      list  = []
    chatter_held_days:   int   = 0

    # Phase 4.10: multi-trigger recovery — "whichever comes first" CB reset
    cooldown_period        = config.get("cooldown_period", 21)          # trading days
    vix_recovery_threshold = config.get("vix_recovery_threshold", 25.0) # VIX level
    vix_recovery_days      = config.get("vix_recovery_days", 3)         # consecutive days
    dd_reset_threshold     = config.get("dd_reset_threshold", 0.03)     # drawdown pct
    cb_trip_day_index: int | None = None       # trading-day index when soft CB tripped
    vix_below_count:   int       = 0           # consecutive days VIX < threshold (soft CB)
    recovery_trigger_count: int  = 0           # how many times recovery fired

    # Phase 4.10: hard CB (binary) recovery tracking — separate state from soft CB
    # hard_cb_trip_day_index: -1 = not yet tripped / just recovered; ≥0 = trip day index
    hard_cb_trip_day_index: int = -1
    hard_vix_below_count:   int = 0

    # Phase 4.8: crisis position limits — restrict new BUYs in HIGH/EXTREME (Change 3)
    use_crisis_pos_limits = config.get("use_crisis_pos_limits", False)
    crisis_max_positions  = config.get("crisis_max_positions",  8)

    # Phase 4.8: minimum dollar risk floor (Change 4)
    # Phase 4.10: dynamic floor scales with cb_mult
    min_dollar_risk_cfg  = config.get("min_dollar_risk", 0)
    signals_below_floor: int = 0
    total_buy_attempts:  int = 0

    # Silent-killer counters: track why trades were skipped
    skipped_vroc:     int = 0
    skipped_min_risk: int = 0

    # Phase 4.8: daily effective_mult tracking for allocation chart
    effective_mult_by_day: list = []

    # Build union calendar: iterate all days where at least one market is open.
    # This ensures Canadian-holiday / US-holiday gaps don't skip valuation days.
    if not ohlcv:
        return _empty_metrics(test_start, test_end)

    test_start_ts = pd.Timestamp(test_start, tz="UTC")
    test_end_ts   = pd.Timestamp(test_end, tz="UTC")
    all_day_set: set[pd.Timestamp] = set()
    for df in ohlcv.values():
        mask = (df.index >= test_start_ts) & (df.index < test_end_ts)
        all_day_set.update(df.index[mask].tolist())
    trading_days = sorted(all_day_set)

    if not trading_days:
        return _empty_metrics(test_start, test_end)

    all_tickers = list(ohlcv.keys())

    # Price forward-filling state: on days a ticker's exchange is closed,
    # carry forward the last known close to avoid $0 valuation (holiday gap fix).
    last_known_close: dict[str, float] = {}

    # Rolling indicator cache: replicates live scanner timing.
    # The live scanner runs at 17:15 ET on day T using T's close data,
    # and trades execute at T+1's Open.  So for a trade on day_ts, the
    # correct indicator snapshot is from the PREVIOUS trading day.
    _scan_ind: dict[str, dict] = {}        # T-1 close indicators (used for signals)
    _scan_prev_ind: dict[str, dict] = {}   # T-2 close indicators (used for crossovers)

    for day_ts in trading_days:
        day_date = day_ts.date()

        # ── 1. Prices for today ──────────────────────────────────────────
        open_prices:  dict[str, float] = {}
        close_prices: dict[str, float] = {}
        trading_today: set[str] = set()   # tickers whose exchange is actually open
        for ticker, df in ohlcv.items():
            if day_ts in df.index:
                open_prices[ticker]  = float(df.at[day_ts, "Open"])
                close_prices[ticker] = float(df.at[day_ts, "Close"])
                trading_today.add(ticker)

        # Forward-fill: carry last known close for tickers not trading today.
        # This prevents positions on closed exchanges from being valued at $0.
        for ticker, last_px in last_known_close.items():
            if ticker not in trading_today:
                close_prices[ticker] = last_px
                open_prices[ticker]  = last_px   # best estimate for valuation
        # Update last known close with today's actual closes
        for t, p in close_prices.items():
            if t in trading_today and p > 0:
                last_known_close[t] = p

        # ── 2. FX rate for today ─────────────────────────────────────────
        if fx_rates is not None:
            fx_today = float(fx_rates.get(day_ts, fx_rates.iloc[-1]))
        else:
            fx_today = 1.0

        # ── 3. Indicator rows ─────────────────────────────────────────────
        today_ind = _build_day_indicators(day_ts, indicators, ohlcv)
        prev_ind  = _build_prev_indicators(day_ts, indicators, ohlcv)

        # scan_ind / scan_prev_ind: the indicator snapshots available at the
        # Open of day_ts.  The live scanner runs at 17:15 the day BEFORE and
        # uses that day's close data, so we replicate the same 1-day lag here.
        # On the very first window-day there is no cached snapshot yet; fall
        # back to prev_ind (the day before day_ts) which is the closest proxy.
        scan_ind      = _scan_ind      if _scan_ind      else prev_ind
        scan_prev_ind = _scan_prev_ind if _scan_prev_ind else prev_ind

        # ── 4. SPY crisis check (last 10 bars before today) ──────────────
        # Use bars strictly before today to match scan_ind timing (T-1 close).
        # The live scanner runs at 17:15 on T-1, so it sees T-1's close data.
        spy_ohlcv = ohlcv.get("SPY")
        spy_crisis_df = None
        if spy_ohlcv is not None:
            spy_hist = spy_ohlcv[spy_ohlcv.index < day_ts].tail(10)
            if not spy_hist.empty:
                spy_crisis_df = spy_hist[["Close"]].rename(columns={"Close": "close"})

        # ── 5. Regime detection ───────────────────────────────────────────
        regimes = detect_all(scan_ind, spy_crisis_df)

        # ── 6. ROC rankings for reversal strategy ─────────────────────────
        all_roc = {
            t: float(row["roc_20"])
            for t, row in scan_ind.items()
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
            e in scan_ind and scan_ind[e].get("roc_20") is not None
            for e in _etfs
        ):
            xle = float(scan_ind["XLE"]["roc_20"])
            xlk = float(scan_ind["XLK"]["roc_20"])
            tlt = float(scan_ind["TLT"]["roc_20"])
            xlu = float(scan_ind["XLU"]["roc_20"])
            xlv = float(scan_ind["XLV"]["roc_20"])
            spy = float(scan_ind["SPY"]["roc_20"])
            sector_scores: dict | None = {
                "late_cycle": (xle - xlk) + (-tlt * 0.5),
                "defensive":  ((xlu - spy) + (xlv - spy)) / 2,
            }
        else:
            sector_scores = None

        # ── 8. Stop-loss / take-profit exits (at Open) ───────────────────
        # Only check SL/TP for tickers whose exchange is actually open today;
        # forward-filled (stale) prices must not trigger exits.
        to_exit: list[tuple[str, float, str]] = []
        for ticker, pos in list(portfolio.positions.items()):
            if ticker not in trading_today:
                continue
            op = open_prices.get(ticker)
            if op is None:
                continue
            if op <= pos["stop_loss"]:
                to_exit.append((ticker, op, "stop_loss"))
            elif op >= pos["take_profit"]:
                to_exit.append((ticker, op, "take_profit"))

        for ticker, exit_px, exit_type in to_exit:
            pos = portfolio.positions[ticker]
            cost_usd = pos["qty"] * pos["avg_cost_usd"]
            _strategy = pos.get("strategy", "unknown")
            _regime = regimes.get(ticker, {}).get("regime", "UNKNOWN").lower()
            pnl = portfolio.close_position(ticker, exit_px, fx_today)
            pnl_pct = pnl / cost_usd if cost_usd > 0 else 0.0
            trade_log.append({"ticker": ticker, "pnl_usd": pnl, "pnl_pct": pnl_pct, "exit_type": exit_type, "strategy": _strategy, "regime": _regime})

        # ── 8a. Circuit breaker state update (after exits, before new buys) ─
        if use_circuit_breaker:
            cb_val   = portfolio.get_total_value(open_prices, fx_today)
            _day_idx = trading_days.index(day_ts)

            # Phase 4.10: VIX lookup for hard-CB time/market recovery
            # Use 3-day trailing average to smooth single-day VIX flickers.
            _hard_vix = None
            if vix_series is not None and not vix_series.empty:
                _vix_ts = pd.Timestamp(day_date, tz="UTC")
                _prior_vix = vix_series[vix_series.index <= _vix_ts]
                if not _prior_vix.empty:
                    _hard_vix = float(_prior_vix.iloc[-3:].mean())

            if _hard_vix is not None and _hard_vix < vix_recovery_threshold:
                hard_vix_below_count += 1
            else:
                hard_vix_below_count = 0

            # Check multi-trigger recovery BEFORE applying dd logic for today
            _hard_recovery = False
            if circuit_breaker_active:
                # Condition 1: Time — penalty box expired since first trip
                if (hard_cb_trip_day_index >= 0 and
                        (_day_idx - hard_cb_trip_day_index) >= cooldown_period):
                    _hard_recovery = True
                # Condition 2: Market — VIX sustained below threshold
                if hard_vix_below_count >= vix_recovery_days:
                    _hard_recovery = True

            if _hard_recovery:
                cb_peak_value          = cb_val   # ATH reset: drawdown now measured from recovery equity
                circuit_breaker_active = False
                hard_cb_trip_day_index = -1       # sentinel cleared; refreshed on next trip
                hard_vix_below_count   = 0
            else:
                current_dd    = (cb_peak_value - cb_val) / cb_peak_value if cb_peak_value > 0 else 0.0
                cb_peak_value = max(cb_peak_value, cb_val)
                if current_dd >= dd_trigger:
                    if not circuit_breaker_active:  # record the first day of this trip only
                        hard_cb_trip_day_index = _day_idx
                    circuit_breaker_active = True
                elif current_dd < dd_reset:
                    circuit_breaker_active = False
                    hard_cb_trip_day_index = -1     # organic dd recovery; reset sentinel

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
            vix_regime_today = vol_state["regime"]
            if vol_state["vix"] is not None:
                vix_day_values.append(vol_state["vix"])
            if vol_state["regime"] in ("HIGH", "EXTREME"):
                elevated_day_count += 1
            if vol_state.get("spike_triggered"):
                spike_day_count += 1
        else:
            size_mult = 1.0
            suppressed_strategies: set[str] = set()
            vix_regime_today = "NORMAL"
            # If soft CB is active but vol filter is off, still get VIX regime for hold period
            if use_soft_cb and vix_series is not None and not vix_series.empty:
                _scb_state = get_vix_regime(
                    day_date, vix_series,
                    sma_window=vroc_window,
                    vroc_threshold=vroc_threshold,
                )
                vix_regime_today = _scb_state.get("regime", "NORMAL")

        # ── 9b. Soft CB state update (Change 1 + 2 + Phase 4.10 recovery) ──
        if use_soft_cb:
            _scb_val   = portfolio.get_total_value(open_prices, fx_today)
            _current_dd = (soft_cb_peak_value - _scb_val) / soft_cb_peak_value if soft_cb_peak_value > 0 else 0.0
            soft_cb_peak_value = max(soft_cb_peak_value, _scb_val)

            # days_since_peak_dd: reset whenever a new drawdown low is reached
            if _current_dd > soft_peak_dd:
                soft_peak_dd       = _current_dd
                days_since_peak_dd = 0
            else:
                days_since_peak_dd += 1

            # VIX-linked minimum recovery hold (Change 2)
            _rec_days_map = {"NORMAL": 3, "ELEVATED": 5, "HIGH": 8, "EXTREME": 12}
            _vix_rec_days = _rec_days_map.get(vix_regime_today, 3)

            # ── Phase 4.10: Multi-trigger recovery ("whichever comes first") ──
            # Track VIX consecutive days below threshold for market-condition trigger.
            # Use 3-day trailing average to smooth single-day VIX flickers.
            _today_vix = None
            if vix_series is not None and not vix_series.empty:
                _vix_ts = pd.Timestamp(day_date, tz="UTC")
                _prior = vix_series[vix_series.index <= _vix_ts]
                if not _prior.empty:
                    _today_vix = float(_prior.iloc[-3:].mean())

            if _today_vix is not None and _today_vix < vix_recovery_threshold:
                vix_below_count += 1
            else:
                vix_below_count = 0

            # Record the day index when CB first tripped (cb_mult < 1.0)
            _day_idx = trading_days.index(day_ts)
            if soft_cb_was_active and cb_trip_day_index is None:
                cb_trip_day_index = _day_idx

            # Check the three recovery conditions (any one resets the CB)
            _recovery_fired = False
            if soft_cb_was_active:
                # Condition 1: Market — VIX sustained below threshold
                if vix_below_count >= vix_recovery_days:
                    _recovery_fired = True
                # Condition 2: Time — penalty box expired
                if cb_trip_day_index is not None and (_day_idx - cb_trip_day_index) >= cooldown_period:
                    _recovery_fired = True
                # Condition 3: Equity — drawdown recovered below reset threshold
                if _current_dd < dd_reset_threshold:
                    _recovery_fired = True

            if _recovery_fired:
                # Full reset: CB releases, ATH resets to current equity
                # so drawdown is measured from recovery level (fixes Re-Trip Trap)
                cb_mult = 1.0
                soft_cb_peak_value = _scb_val
                soft_cb_was_active = False
                cb_trip_day_index  = None
                vix_below_count    = 0
                soft_peak_dd       = 0.0
                days_since_peak_dd = 0
                recovery_trigger_count += 1
            elif _current_dd < cb_soft_start:
                if soft_cb_was_active and days_since_peak_dd < _vix_rec_days:
                    # Chatter prevention: hold at last linear value, don't release yet
                    cb_mult = last_cb_mult
                    chatter_held_days += 1
                else:
                    cb_mult = 1.0
                    soft_cb_was_active = False
            elif _current_dd < cb_hard_stop:
                _band   = cb_hard_stop - cb_soft_start
                cb_mult = 1.0 - ((1.0 - cb_min_mult) * (_current_dd - cb_soft_start) / _band)
                soft_cb_was_active = True
            else:
                cb_mult = 0.0
                soft_cb_was_active = True

            last_cb_mult = cb_mult
        elif use_circuit_breaker:
            # Legacy binary CB: cb_mult is 0 when breaker active, 1 otherwise
            cb_mult = 0.0 if circuit_breaker_active else 1.0
        else:
            cb_mult = 1.0

        cb_mult_values.append(cb_mult)

        # ── 9c. Effective max positions (crisis limits, Change 3) ──────────
        if use_crisis_pos_limits and vix_regime_today in ("HIGH", "EXTREME"):
            effective_max_positions = crisis_max_positions
        else:
            effective_max_positions = max_open_positions

        # Day-level effective multiplier for allocation chart
        effective_mult_by_day.append((day_date, size_mult * cb_mult))

        # ── 10. Generate signals from all strategies ──────────────────────
        # Use scan_ind (T-1 close) so signal generation matches the live
        # scanner which runs after close and fills at next-day Open.
        all_signals: list[dict] = []
        all_signals.extend(rsi_strategy.generate_signals(
            all_tickers, scan_ind, regimes, open_tickers))
        all_signals.extend(momentum_strategy.generate_signals(
            all_tickers, scan_ind, scan_prev_ind, regimes, open_tickers))
        all_signals.extend(macd_strategy.generate_signals(
            all_tickers, scan_ind, scan_prev_ind, regimes, open_tickers))
        all_signals.extend(reversal_strategy.generate_signals(
            all_tickers, scan_ind, regimes, open_tickers, roc_rankings))
        all_signals.extend(sector_rotation_strategy.generate_signals(
            all_tickers, scan_ind, regimes, open_tickers, sector_scores, sector_map))

        # Build a sell-signal lookup for reversal exit checks (step 13b)
        sell_signals_today: dict[str, dict] = {}
        for _sig in all_signals:
            if str(_sig.get("signal_type", "")).upper() == "SELL":
                _st = _sig.get("ticker", "")
                if _st:
                    sell_signals_today[_st] = _sig

        # Keep only BUY signals for the buy loop
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
            _spike_active = use_vol and vol_state.get("spike_triggered", False)
            _vroc_ratio   = vol_state.get("vroc_ratio", 0.0) if use_vol else 0.0
            for sig in buy_signals:
                if sig.get("strategy") in suppressed_strategies:
                    sig["strength"] = 0.0
                    skipped_vroc += 1

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

        # ── 13b. Signal reversal exits ────────────────────────────────────
        # Mirror position_manager.py exits 4 (RSI reversal + momentum death cross).
        # Detected from T-1 indicators (scan_ind); exits at today's Open price.
        for ticker, pos in list(portfolio.positions.items()):
            if ticker not in trading_today:
                continue
            exit_px = open_prices.get(ticker)
            if exit_px is None:
                continue
            _strat   = pos.get("strategy", "")
            _exited  = False

            # RSI reversal: sell signal with strength > 0.5
            if _strat == "rsi" and ticker in sell_signals_today:
                sig_strength = float(sell_signals_today[ticker].get("strength", 0.0))
                if sig_strength > 0.5:
                    cost_usd = pos["qty"] * pos["avg_cost_usd"]
                    _regime  = regimes.get(ticker, {}).get("regime", "UNKNOWN").lower()
                    pnl = portfolio.close_position(ticker, exit_px, fx_today)
                    trade_log.append({"ticker": ticker, "pnl_usd": pnl,
                                      "pnl_pct": pnl / cost_usd if cost_usd > 0 else 0.0,
                                      "exit_type": "signal_reversal", "strategy": _strat, "regime": _regime})
                    _exited = True

            # Momentum death cross: ma_50 < ma_200 in T-1 indicators
            if not _exited and _strat == "momentum":
                _ind   = scan_ind.get(ticker, {})
                ma_50  = _ind.get("ma_50")
                ma_200 = _ind.get("ma_200")
                if ma_50 is not None and ma_200 is not None and float(ma_50) < float(ma_200):
                    cost_usd = pos["qty"] * pos["avg_cost_usd"]
                    _regime  = regimes.get(ticker, {}).get("regime", "UNKNOWN").lower()
                    pnl = portfolio.close_position(ticker, exit_px, fx_today)
                    trade_log.append({"ticker": ticker, "pnl_usd": pnl,
                                      "pnl_pct": pnl / cost_usd if cost_usd > 0 else 0.0,
                                      "exit_type": "signal_reversal", "strategy": _strat, "regime": _regime})

        # ── 13c. Time-based exits (> 30 days open, negative P&L) ─────────
        # Mirror position_manager.py exit 5. Exit at today's Open price.
        for ticker, pos in list(portfolio.positions.items()):
            if ticker not in trading_today:
                continue
            opened = pos.get("opened_date")
            if opened is None:
                continue
            if (day_date - opened).days <= 30:
                continue
            exit_px = open_prices.get(ticker)
            if exit_px is None:
                continue
            # Compute P&L in native currency to match live system's pnl < 0 check
            avg_native = pos["avg_cost_usd"] / (fx_today if pos["is_cad"] else 1.0)
            if (exit_px - avg_native) * pos["qty"] < 0:
                cost_usd = pos["qty"] * pos["avg_cost_usd"]
                _regime  = regimes.get(ticker, {}).get("regime", "UNKNOWN").lower()
                _strat   = pos.get("strategy", "unknown")
                pnl = portfolio.close_position(ticker, exit_px, fx_today)
                trade_log.append({"ticker": ticker, "pnl_usd": pnl,
                                  "pnl_pct": pnl / cost_usd if cost_usd > 0 else 0.0,
                                  "exit_type": "time_exit", "strategy": _strat, "regime": _regime})

        # ── 14. Execute trades ────────────────────────────────────────────
        just_opened: set[str] = set()
        for sig in buy_signals:
            tkr      = sig["ticker"]
            strength = float(sig["strength"])
            strategy_name = sig.get("strategy", "unknown")

            if tkr in just_opened or tkr in portfolio.positions:
                continue

            # Only open positions on tickers whose exchange is open today
            if tkr not in trading_today:
                continue
            fill_price = open_prices.get(tkr)
            if fill_price is None or fill_price <= 0:
                continue

            atr = scan_ind.get(tkr, {}).get("atr_14")
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

            total_val      = portfolio.get_total_value(close_prices, fx_today)
            effective_mult  = size_mult * cb_mult
            if effective_mult == 0.0:
                continue  # hard stop: no new BUYs (soft CB or binary CB)
            dollar_risk = strength * portfolio.max_portfolio_risk * total_val * effective_mult
            total_buy_attempts += 1
            # Phase 4.10: dynamic risk floor scales with CB multiplier
            effective_min_risk = min_dollar_risk_cfg * max(effective_mult, 0.01)
            if dollar_risk < effective_min_risk:
                signals_below_floor += 1
                skipped_min_risk += 1
                continue
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
                effective_max_positions, max_sector_exposure,
            )
            if not allowed:
                continue

            portfolio.open_position(
                tkr, qty, fill_price, stop_loss, take_profit,
                strategy_name, sector, fx_today,
                opened_date=day_date,
            )
            just_opened.add(tkr)

        # ── 15. Record equity curve + daily snapshot ────────────────────
        # ── 15b. Trailing stop ratchet ────────────────────────────────────
        # Mirror position_manager.py exit 3. Uses today's Close price and ATR.
        # Updated stop will be checked against NEXT day's Open (step 8).
        for ticker, pos in portfolio.positions.items():
            if ticker not in trading_today:
                continue
            close_px = close_prices.get(ticker)
            if close_px is None:
                continue
            avg_native = pos["avg_cost_usd"] / (fx_today if pos["is_cad"] else 1.0)
            if close_px <= avg_native * 1.05:
                continue  # need >5% profit before ratcheting
            atr = today_ind.get(ticker, {}).get("atr_14")
            if not atr:
                continue
            try:
                atr = float(atr)
            except (TypeError, ValueError):
                continue
            if atr == 0:
                continue
            new_stop = close_px - portfolio.atr_multiplier * atr
            if new_stop > pos["stop_loss"]:
                portfolio.positions[ticker]["stop_loss"] = new_stop

        total_val = portfolio.get_total_value(close_prices, fx_today)
        if total_val > portfolio.peak_value:
            portfolio.peak_value = total_val
        equity_curve.append((day_date, total_val))

        # Snapshot: positions held, cash, total value, total P&L
        snap_positions = {}
        for _t, _p in portfolio.positions.items():
            _px = close_prices.get(_t, 0.0)
            _mv = _p["qty"] * _px * (fx_today if _p["is_cad"] else 1.0)
            snap_positions[_t] = {
                "qty": _p["qty"],
                "avg_cost_usd": round(_p["avg_cost_usd"], 2),
                "market_value_usd": round(_mv, 2),
                "strategy": _p["strategy"],
                "sector": _p["sector"],
            }
        daily_snapshots.append({
            "date": day_date.isoformat(),
            "positions": snap_positions,
            "cash": round(portfolio.cash, 2),
            "total_value": round(total_val, 2),
            "total_pnl": round(total_val - portfolio.initial_capital, 2),
        })

        # ── 16. Advance rolling indicator cache for next iteration ────────
        # _scan_ind becomes the indicators from today's close; on the next
        # trading day these will be the "prior-day" signals the live scanner
        # would have generated overnight.
        _scan_prev_ind = _scan_ind
        _scan_ind      = today_ind

    # ── Force-close any remaining positions at last Close ─────────────────
    if equity_curve:
        last_day_ts = trading_days[-1]
        last_close  = {t: float(df.at[last_day_ts, "Close"])
                       for t, df in ohlcv.items() if last_day_ts in df.index}
        # Use forward-filled prices for tickers whose exchange was closed on the last day
        for t, p in last_known_close.items():
            if t not in last_close:
                last_close[t] = p
        final_fx = float(fx_rates.iloc[-1]) if fx_rates is not None else 1.0
        for ticker in list(portfolio.positions.keys()):
            ep = last_close.get(ticker, 0.0)
            if ep > 0:
                pos = portfolio.positions[ticker]
                cost_usd = pos["qty"] * pos["avg_cost_usd"]
                _strategy = pos.get("strategy", "unknown")
                pnl = portfolio.close_position(ticker, ep, final_fx)
                pnl_pct = pnl / cost_usd if cost_usd > 0 else 0.0
                trade_log.append({"ticker": ticker, "pnl_usd": pnl, "pnl_pct": pnl_pct, "exit_type": "end_of_window", "strategy": _strategy, "regime": "unknown"})

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

    # Phase 4.8: soft CB + additional risk control metrics
    metrics["use_soft_cb"]            = use_soft_cb
    metrics["cb_soft_start"]          = cb_soft_start if use_soft_cb else None
    metrics["cb_hard_stop"]           = cb_hard_stop  if use_soft_cb else None
    metrics["cb_min_mult"]            = cb_min_mult   if use_soft_cb else None
    metrics["avg_cb_mult"]            = float(np.mean(cb_mult_values)) if cb_mult_values else None
    metrics["pct_days_chatter_held"]  = (
        chatter_held_days / n_days if (use_soft_cb and n_days > 0) else None
    )
    metrics["use_crisis_pos_limits"]  = use_crisis_pos_limits
    metrics["crisis_max_positions"]   = crisis_max_positions if use_crisis_pos_limits else None
    metrics["min_dollar_risk"]        = min_dollar_risk_cfg
    metrics["pct_signals_below_floor"] = (
        signals_below_floor / total_buy_attempts if total_buy_attempts > 0 else None
    )
    metrics["effective_mult_by_day"]  = effective_mult_by_day  # per-day, not persisted to DB
    metrics["skipped_vroc"]            = skipped_vroc
    metrics["skipped_min_risk"]        = skipped_min_risk

    # Phase 4.10: multi-trigger recovery metrics
    metrics["cooldown_period"]         = cooldown_period if use_soft_cb else None
    metrics["vix_recovery_threshold"]  = vix_recovery_threshold if use_soft_cb else None
    metrics["vix_recovery_days"]       = vix_recovery_days if use_soft_cb else None
    metrics["dd_reset_threshold"]      = dd_reset_threshold if use_soft_cb else None
    metrics["recovery_trigger_count"]  = recovery_trigger_count if use_soft_cb else None

    # Phase 4.9: trade log with strategy+regime for analytics computation
    metrics["trade_log"] = trade_log

    # Daily portfolio snapshots for analytics
    metrics["daily_snapshots"] = daily_snapshots

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
        # Phase 4.8 soft CB + additional controls
        "use_soft_cb":             False,
        "cb_soft_start":           None,
        "cb_hard_stop":            None,
        "cb_min_mult":             None,
        "avg_cb_mult":             None,
        "pct_days_chatter_held":   None,
        "use_crisis_pos_limits":   False,
        "crisis_max_positions":    None,
        "min_dollar_risk":         0,
        "pct_signals_below_floor": None,
        "effective_mult_by_day":   [],
        "skipped_vroc":            0,
        "skipped_min_risk":        0,
        # Phase 4.10 multi-trigger recovery fields
        "cooldown_period":         None,
        "vix_recovery_threshold":  None,
        "vix_recovery_days":       None,
        "dd_reset_threshold":      None,
        "recovery_trigger_count":  None,
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
# Analytics computation (Phase 4.9)
# ---------------------------------------------------------------------------

def compute_analytics(metrics: dict) -> dict:
    """Build chart-ready analytics from a run_window() result dict.

    Returns dict with keys: equity_curve, drawdown_curve, monthly_returns,
    regime_stats, timestamps.
    """
    ec = metrics.get("equity_curve", [])
    trade_log = metrics.get("trade_log", [])

    if not ec:
        return {
            "equity_curve": [],
            "drawdown_curve": [],
            "monthly_returns": {},
            "regime_stats": {},
            "timestamps": [],
        }

    # --- Timestamps & equity values ---
    timestamps = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d, _ in ec]
    values = [float(v) for _, v in ec]

    # --- Drawdown curve ---
    peak = values[0]
    drawdown_curve = []
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        drawdown_curve.append(round(-dd, 6))

    # --- Monthly returns ---
    _MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    monthly_returns: dict[str, dict] = {}
    if len(ec) >= 2:
        # Group equity values by (year, month) → first and last value
        from collections import OrderedDict
        month_buckets: dict[tuple[int, int], list[tuple]] = OrderedDict()
        for d, v in ec:
            if hasattr(d, "year"):
                yr, mo = d.year, d.month
            else:
                # ISO string fallback
                parts = str(d).split("-")
                yr, mo = int(parts[0]), int(parts[1])
            key = (yr, mo)
            if key not in month_buckets:
                month_buckets[key] = []
            month_buckets[key].append(float(v))

        # Convert to monthly returns: (last_val / first_val) - 1
        prev_end_val = None
        for (yr, mo), vals in month_buckets.items():
            yr_str = str(yr)
            if yr_str not in monthly_returns:
                monthly_returns[yr_str] = {}
            start_val = prev_end_val if prev_end_val is not None else vals[0]
            end_val = vals[-1]
            ret = (end_val / start_val - 1) if start_val > 0 else 0.0
            monthly_returns[yr_str][_MONTHS[mo - 1]] = round(ret, 4)
            prev_end_val = end_val

        # Add annual return per year
        for yr_str, months in monthly_returns.items():
            annual = 1.0
            for m in _MONTHS:
                if m in months:
                    annual *= (1 + months[m])
            monthly_returns[yr_str]["annual"] = round(annual - 1, 4)

    # --- Regime stats: strategy → regime → net PnL ---
    regime_stats: dict[str, dict[str, float]] = {}
    for t in trade_log:
        strat = t.get("strategy", "unknown")
        regime = t.get("regime", "unknown")
        pnl = float(t.get("pnl_usd", 0))
        if strat not in regime_stats:
            regime_stats[strat] = {}
        regime_stats[strat][regime] = regime_stats[strat].get(regime, 0.0) + pnl

    # Round regime_stats values
    for strat in regime_stats:
        for reg in regime_stats[strat]:
            regime_stats[strat][reg] = round(regime_stats[strat][reg], 2)

    return {
        "equity_curve": [round(v, 2) for v in values],
        "drawdown_curve": drawdown_curve,
        "monthly_returns": monthly_returns,
        "regime_stats": regime_stats,
        "timestamps": timestamps,
        "daily_snapshots": metrics.get("daily_snapshots", []),
    }


# ---------------------------------------------------------------------------
# ROOS orchestrator
# ---------------------------------------------------------------------------

def run_roos(
    config: dict,
    config_name: str,
    windows: list[dict],
    tickers: list[str],
    sector_map: dict[str, str],
    progress_prefix: str = "",
    capital_mode: str = "capital_refresh",
    preloaded_data: dict | None = None,
    quiet: bool = False,
) -> list[dict]:
    """Run all ROOS windows for one allocation config.

    Data for the full date range is loaded once (not per window) so overlapping
    windows share the same in-memory DataFrames. Each window gets a slice.

    Parameters
    ----------
    capital_mode:
        'capital_refresh'    — fresh INITIAL_CAPITAL per window (default).
        'capital_compounded' — single continuous simulation across the full
                               date range (first test_start → last test_end).
    preloaded_data:
        Optional dict with pre-loaded DataFrames to skip DB queries.
        Keys: 'ohlcv', 'indicators', 'fx_rates', 'vix' (vix may be None).
        When provided, data loading is skipped entirely.

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

    if preloaded_data is not None:
        ohlcv_all      = preloaded_data["ohlcv"]
        indicators_all = preloaded_data["indicators"]
        fx_rates_all   = preloaded_data["fx_rates"]
        vix_all        = preloaded_data.get("vix")
    else:
        if _rich and not quiet:
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

    # ------------------------------------------------------------------
    # capital_compounded: single continuous run across the full date range
    # ------------------------------------------------------------------
    if capital_mode == "capital_compounded":
        test_start = windows[0]["test_start"]
        test_end   = windows[-1]["test_end"]

        if _rich and not quiet:
            out = Console()
            out.print(f"  {progress_prefix}[bold]{config_name}[/bold]  {test_start} → {test_end} (compounded)")

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
        metrics["config_name"]   = config_name
        metrics["config"]        = config
        metrics["window_index"]  = 1
        metrics["category"]      = "roos"
        metrics["capital_mode"]  = capital_mode
        return [metrics]

    # ------------------------------------------------------------------
    # capital_refresh: independent window per ROOS period (original)
    # ------------------------------------------------------------------
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
        if (_rich and not quiet) else None
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

        # Slice to test window (keep a 20-day buffer before test_start for prev_indicators)
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
        metrics["category"]     = "roos"
        metrics["capital_mode"]  = capital_mode
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
    """Run all allocation configs across all ROOS windows, store to DB.

    Returns a summary DataFrame.
    """
    from db.connection import insert_backtest_result, insert_backtest_analytics, get_backtest_summary
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

        window_results = run_roos(
            config, config_name, windows, tickers, sector_map,
            progress_prefix=f"[{idx}/{n_configs}] ",
        )

        # Persist each window result — each config gets its own run_id
        run_id = str(uuid.uuid4())
        for res in window_results:
            db_row = {k: v for k, v in res.items() if k not in ("equity_curve", "circuit_breaker_days", "effective_mult_by_day", "trade_log", "daily_snapshots")}
            db_row["currency"] = currency
            db_row["run_id"]   = run_id
            bt_id = insert_backtest_result(db_row)
            analytics = compute_analytics(res)
            insert_backtest_analytics(bt_id, analytics)

    return get_backtest_summary()
