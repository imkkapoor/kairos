"""
simulator/simulator.py — Main paper-trading orchestrator.

TWO daily jobs (scheduled via scheduler.py):

  run_morning()  — 9:31 AM ET weekdays
    Fetches live prices in one batch, executes signals from last night's scan.
    THIS IS THE ONLY PLACE new trades are opened.

  run_evening()  — 5:25 PM ET weekdays
    Uses today's closing prices from price_data DB (no yfinance call).
    Manages existing positions: stop-loss hits, take-profits, trailing stops.
    Does NOT open new trades.

THIS IS THE ONLY FILE in simulator/ that calls DB write functions.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import (
    get_todays_signals,
    get_latest_indicators,
    get_latest_close_prices,
    close_trade,
    insert_trade,
    insert_fx_rate,
    get_fx_rate,
    mark_signal_acted_on,
    update_stop_loss,
    get_sector_map,
    get_market_map,
)
from simulator.portfolio import Portfolio
from simulator import executor
from simulator import position_manager

_ET = ZoneInfo("America/New_York")


def _last_scan_date_utc() -> datetime:
    """Return midnight UTC for the most recent weekday in ET.

    The scanner runs at 5:15 PM ET. At 9:31 AM ET the next morning, its
    signals sit in the *previous* ET calendar day's UTC window (signals are
    written ~21-22 UTC, run_morning fires ~13-14 UTC the next day).
    Walking back to the last weekday also handles Monday mornings correctly
    (scanner last ran Friday, not yesterday).
    """
    now_et = datetime.now(_ET)
    days_back = 1
    while True:
        candidate = (now_et - timedelta(days=days_back)).date()
        if candidate.weekday() < 5:   # Mon=0 … Fri=4
            break
        days_back += 1
    return datetime(candidate.year, candidate.month, candidate.day, tzinfo=timezone.utc)


def run_morning() -> None:
    """9:31 AM ET — fetch live prices in one batch, execute last night's signals."""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d")
    logger.info(f"=== Kairos morning execution starting for {now_str} ===")

    # ------------------------------------------------------------------
    # 1. Load portfolio
    # ------------------------------------------------------------------
    portfolio = Portfolio.load_from_db()

    # ------------------------------------------------------------------
    # 2. Load last night's signals, sorted by z_score DESC
    #    Signals are written at ~17:15 ET (21-22 UTC) by the previous
    #    evening's scanner, so they live in yesterday's UTC date window.
    # ------------------------------------------------------------------
    scan_date = _last_scan_date_utc()
    logger.info(f"run_morning: loading signals for scan date {scan_date.date()} UTC")
    signals_df = get_todays_signals(for_date=scan_date)
    if signals_df.empty:
        logger.info("run_morning: No signals to execute")
        print(f"-- Kairos morning execution -- {now_str} 09:31 ET --")
        print("No signals to execute.")
        print("------------------------------------------")
        return

    todays_signals: list[dict] = signals_df.to_dict("records")
    todays_signals.sort(
        key=lambda s: float(s.get("z_score") if s.get("z_score") is not None else s.get("strength", 0.0)),
        reverse=True,
    )

    # ------------------------------------------------------------------
    # 3. Fetch live prices in ONE batch for signals + open positions
    # ------------------------------------------------------------------
    signal_tickers: set[str] = {s["ticker"] for s in todays_signals if s.get("ticker")}
    position_tickers: set[str] = set(portfolio.positions.keys())
    tickers_needed: list[str] = list(signal_tickers | position_tickers)

    logger.info(f"run_morning: fetching live prices for {len(tickers_needed)} tickers")
    current_prices = executor.fetch_live_prices(tickers_needed)

    # Pin all trade timestamps to 9:31 AM ET (same anchor as fill prices)
    # so manual runs and the scheduled job produce identical trade rows.
    trade_time = executor._open_cutoff_utc()

    # ------------------------------------------------------------------
    # 4. Load today's indicators (written last night by scanner)
    # ------------------------------------------------------------------
    today_indicators = get_latest_indicators(tickers_needed) if tickers_needed else {}

    # ------------------------------------------------------------------
    # 5. Fetch sector map + market map + FX rates
    # ------------------------------------------------------------------
    sector_map = get_sector_map()
    market_map = get_market_map()

    portfolio_ccy = portfolio.currency  # e.g. 'CAD'
    fx_rates: dict[str, float] = {portfolio_ccy: 1.0}
    today_start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Determine which foreign currencies are needed
    needed_ccys = {executor.get_ticker_currency(t, market_map) for t in tickers_needed}
    for ccy in needed_ccys:
        if ccy != portfolio_ccy and ccy not in fx_rates:
            pair = f"{ccy}{portfolio_ccy}"
            rate = get_fx_rate(pair, since=today_start_utc)
            if rate is None:
                # Not yet in DB for today — fetch live once and persist
                rate = executor.fetch_fx_rate(ccy, portfolio_ccy)
                if rate is not None:
                    insert_fx_rate(pair, rate)
            if rate is not None:
                fx_rates[ccy] = rate
            else:
                logger.warning(f"run_morning: could not get {ccy}→{portfolio_ccy} rate, using 1.0")
                fx_rates[ccy] = 1.0
    logger.info(f"run_morning: FX rates ({portfolio_ccy} base): {fx_rates}")

    # ------------------------------------------------------------------
    # 6. Execute new signals (buy/sell) — no position management here
    # ------------------------------------------------------------------
    already_held = set(portfolio.positions.keys())
    eligible_signals = [
        s for s in todays_signals
        if s.get("signal_type", "").lower() != "buy" or s.get("ticker") not in already_held
    ]

    executed, skipped = executor.execute_signals(
        eligible_signals, portfolio, current_prices, today_indicators, sector_map,
        market_map=market_map, fx_rates=fx_rates,
    )

    summary_opened: list[dict] = []
    for trade in executed:
        if trade["side"] == "buy":
            insert_trade(
                ticker          = trade["ticker"],
                side            = "buy",
                quantity        = trade["quantity"],
                fill_price      = trade["fill_price"],
                stop_loss       = trade["stop_loss"],
                take_profit     = trade["take_profit"],
                strategy        = trade["strategy"],
                signal_strength = trade["signal_strength"],
                reason          = trade["reason"],
                signal_data     = trade.get("signal_data"),
                trade_time      = trade_time,
                fill_type       = trade.get("fill_type"),
            )
            signal_id = trade.get("signal_id")
            if signal_id is not None:
                try:
                    mark_signal_acted_on(int(signal_id))
                except Exception as exc:
                    logger.warning(f"mark_signal_acted_on({signal_id}): {exc}")
            summary_opened.append({
                "ticker":      trade["ticker"],
                "qty":         trade["quantity"],
                "fill_price":  trade["fill_price"],
                "stop_loss":   trade["stop_loss"],
                "take_profit": trade["take_profit"],
                "strategy":    trade["strategy"],
                "strength":    trade["signal_strength"],
                "currency":    trade.get("currency", "USD"),
                "fx_rate":     trade.get("fx_rate", 1.0),
            })
        elif trade["side"] == "sell":
            close_trade(trade["ticker"], trade["fill_price"], trade["reason"], trade_time=trade_time)
            signal_id = trade.get("signal_id")
            if signal_id is not None:
                try:
                    mark_signal_acted_on(int(signal_id))
                except Exception as exc:
                    logger.warning(f"mark_signal_acted_on({signal_id}): {exc}")

    portfolio.snapshot(current_prices, fx_rates=fx_rates, market_map=market_map)

    # ------------------------------------------------------------------
    # Print morning summary
    # ------------------------------------------------------------------
    total_value = portfolio.get_total_value(current_prices, fx_rates, market_map)
    open_count  = len(portfolio.positions)

    print()
    print(f"-- Kairos morning execution -- {now_str} 09:31 ET --")
    print(f"Opened today ({len(summary_opened)}):")
    if summary_opened:
        for t in summary_opened:
            ccy_label = t.get('currency', '')
            fx_label  = f" fx={t['fx_rate']:.4f}" if t.get('fx_rate', 1.0) != 1.0 else ""
            print(
                f"  {t['ticker']:8s} BUY  {t['qty']:.0f}sh @ ${t['fill_price']:.2f} {ccy_label}{fx_label}  "
                f"SL:${t['stop_loss']:.2f}  TP:${t['take_profit']:.2f}  str:{t['strength']:.2f}"
            )
    else:
        print("  (none)")
    print(f"Skipped ({len(skipped)}):", end=" ")
    if skipped:
        print()
        for s in skipped:
            print(f"  {s['ticker']:8s} {s['reason']}")
    else:
        print("(none)")
    print(
        f"Portfolio: ${total_value:,.2f} {portfolio.currency}  |  "
        f"Cash: ${portfolio.cash:,.2f} {portfolio.currency}  |  "
        f"Open: {open_count}/{portfolio.MAX_OPEN_POSITIONS}"
    )
    if fx_rates:
        fx_parts = [f"{c}={r:.4f}" for c, r in fx_rates.items() if c != portfolio.currency]
        if fx_parts:
            print(f"FX rates: {', '.join(fx_parts)}")
    print("------------------------------------------")
    logger.info(
        f"run_morning complete: opened={len(summary_opened)}, skipped={len(skipped)}"
    )


def run_evening() -> None:
    """5:25 PM ET — check stops/take-profits using today's close prices from DB."""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d")
    logger.info(f"=== Kairos evening management starting for {now_str} ===")

    # ------------------------------------------------------------------
    # 1. Load portfolio
    # ------------------------------------------------------------------
    portfolio = Portfolio.load_from_db()

    if not portfolio.positions:
        logger.info("run_evening: no open positions — nothing to manage")
        print(f"-- Kairos evening management -- {now_str} 17:25 ET --")
        print("No open positions.")
        print("------------------------------------------")
        return

    # ------------------------------------------------------------------
    # 2. Fetch today's closing prices from price_data DB (no yfinance call)
    # ------------------------------------------------------------------
    position_tickers = list(portfolio.positions.keys())
    closing_prices = get_latest_close_prices(position_tickers)

    missing = [t for t in position_tickers if t not in closing_prices]
    if missing:
        logger.warning(f"run_evening: no close price in DB for: {missing}")

    # ------------------------------------------------------------------
    # 2b. Market map + FX rates for currency conversion
    # ------------------------------------------------------------------
    market_map = get_market_map()
    portfolio_ccy = portfolio.currency
    fx_rates: dict[str, float] = {portfolio_ccy: 1.0}
    today_start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    needed_ccys = {executor.get_ticker_currency(t, market_map) for t in position_tickers}
    for ccy in needed_ccys:
        if ccy != portfolio_ccy and ccy not in fx_rates:
            pair = f"{ccy}{portfolio_ccy}"
            rate = get_fx_rate(pair, since=today_start_utc)
            if rate is None:
                # Not in DB for today — fetch live once and persist
                rate = executor.fetch_fx_rate(ccy, portfolio_ccy)
                if rate is not None:
                    insert_fx_rate(pair, rate)
            if rate is not None:
                fx_rates[ccy] = rate
            else:
                logger.warning(f"run_evening: could not get {ccy}→{portfolio_ccy} rate, using 1.0")
                fx_rates[ccy] = 1.0
    logger.info(f"run_evening: FX rates ({portfolio_ccy} base): {fx_rates}")

    # ------------------------------------------------------------------
    # 3. Load indicators and today's signals
    # ------------------------------------------------------------------
    today_indicators = get_latest_indicators(position_tickers) if position_tickers else {}
    signals_df = get_todays_signals(for_date=now)
    todays_signals: list[dict] = [] if signals_df.empty else signals_df.to_dict("records")

    # ------------------------------------------------------------------
    # 4. Position management: close exits + trailing stop updates
    # ------------------------------------------------------------------
    close_actions, trailing_stop_updates = position_manager.check_positions(
        portfolio, closing_prices, today_indicators, todays_signals
    )

    summary_closed: list[dict] = []
    for action in close_actions:
        ticker     = action["ticker"]
        exit_price = action["exit_price"]
        reason     = action["reason"]
        pnl = portfolio.close_position(ticker, exit_price)
        close_trade(ticker, exit_price, reason)
        summary_closed.append({
            "ticker":     ticker,
            "exit_price": exit_price,
            "reason":     reason,
            "pnl":        pnl,
        })

    for item in trailing_stop_updates:
        update_stop_loss(item["ticker"], item["new_stop_loss"])

    portfolio.snapshot(closing_prices, fx_rates=fx_rates, market_map=market_map)

    # ------------------------------------------------------------------
    # Print evening summary
    # ------------------------------------------------------------------
    total_value = portfolio.get_total_value(closing_prices, fx_rates, market_map)
    drawdown    = portfolio.get_drawdown(total_value)

    print()
    print(f"-- Kairos evening management -- {now_str} 17:25 ET --")
    print(f"Closed today ({len(summary_closed)}):")
    if summary_closed:
        for t in summary_closed:
            pnl_val = t['pnl']
            pnl_str = f"{'+' if pnl_val >= 0 else ''}${pnl_val:,.2f}"
            print(f"  {t['ticker']:8s} SELL @ ${t['exit_price']:.2f}  P&L: {pnl_str}  {t['reason']}")
    else:
        print("  (none)")
    print(f"Trailing stops updated ({len(trailing_stop_updates)}):")
    if trailing_stop_updates:
        for u in trailing_stop_updates:
            print(f"  {u['ticker']:8s} new SL: ${u['new_stop_loss']:.2f}")
    else:
        print("  (none)")
    print(
        f"Portfolio: ${total_value:,.2f} {portfolio.currency}  |  "
        f"Drawdown: {drawdown:.2%}"
    )
    if fx_rates:
        fx_parts = [f"{c}={r:.4f}" for c, r in fx_rates.items() if c != portfolio.currency]
        if fx_parts:
            print(f"FX rates: {', '.join(fx_parts)}")
    print("------------------------------------------")
    logger.info(
        f"run_evening complete: closed={len(summary_closed)}, "
        f"trailing_updates={len(trailing_stop_updates)}"
    )


def run_intraday() -> None:
    """Hourly 10:00–15:58 ET — live price check; manage stops/TPs/trailing stops.

    Fetches live 1-minute bar prices for all open positions, runs
    position_manager, persists any closes/trailing-stop updates to the DB,
    and takes a portfolio snapshot only when something changed.
    Does NOT open new trades.
    """
    now    = datetime.now(timezone.utc)
    now_et = now.astimezone(_ET)
    now_str = now_et.strftime("%Y-%m-%d %H:%M ET")
    logger.info(f"=== Kairos intraday management starting {now_str} ===")

    # ------------------------------------------------------------------
    # 1. Load portfolio
    # ------------------------------------------------------------------
    portfolio = Portfolio.load_from_db()

    if not portfolio.positions:
        logger.info("run_intraday: no open positions — nothing to manage")
        return

    position_tickers = list(portfolio.positions.keys())

    # ------------------------------------------------------------------
    # 2. Fetch live prices right now (no 9:31 cutoff)
    # ------------------------------------------------------------------
    current_prices = executor.fetch_current_prices(position_tickers)

    if not current_prices:
        logger.warning(
            "run_intraday: no live prices returned for any position "
            "(market closed / holiday?) — skipping this run"
        )
        return

    missing = [t for t in position_tickers if t not in current_prices]
    if missing:
        logger.warning(f"run_intraday: no live price for: {missing}")
        # Fill gaps (e.g. TSX-listed tickers on Canadian holidays) with the
        # last known close from the DB so the snapshot uses a real price,
        # not the at-cost fallback in get_total_value().
        last_close = get_latest_close_prices(missing)
        for t, price in last_close.items():
            current_prices[t] = price
            logger.info(f"run_intraday: using last DB close for {t}: {price:.4f}")

    # ------------------------------------------------------------------
    # 3. FX rates from DB (fallback to live fetch + persist if absent)
    # ------------------------------------------------------------------
    market_map    = get_market_map()
    portfolio_ccy = portfolio.currency
    fx_rates: dict[str, float] = {portfolio_ccy: 1.0}
    today_start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    needed_ccys = {executor.get_ticker_currency(t, market_map) for t in position_tickers}
    for ccy in needed_ccys:
        if ccy != portfolio_ccy and ccy not in fx_rates:
            pair = f"{ccy}{portfolio_ccy}"
            rate = get_fx_rate(pair, since=today_start_utc)
            if rate is None:
                rate = executor.fetch_fx_rate(ccy, portfolio_ccy)
                if rate is not None:
                    insert_fx_rate(pair, rate)
            if rate is not None:
                fx_rates[ccy] = rate
            else:
                logger.warning(f"run_intraday: could not get {ccy}→{portfolio_ccy} rate, using 1.0")
                fx_rates[ccy] = 1.0

    # ------------------------------------------------------------------
    # 4. Indicators + today's signals for position_manager
    # ------------------------------------------------------------------
    today_indicators = get_latest_indicators(position_tickers)
    signals_df       = get_todays_signals(for_date=now)
    todays_signals: list[dict] = [] if signals_df.empty else signals_df.to_dict("records")

    # ------------------------------------------------------------------
    # 5. Check positions for exits / trailing stop updates
    # ------------------------------------------------------------------
    close_actions, trailing_stop_updates = position_manager.check_positions(
        portfolio, current_prices, today_indicators, todays_signals
    )

    summary_closed: list[dict] = []
    for action in close_actions:
        ticker     = action["ticker"]
        exit_price = action["exit_price"]
        reason     = action["reason"]
        pnl = portfolio.close_position(ticker, exit_price)
        close_trade(ticker, exit_price, reason)
        summary_closed.append({
            "ticker":     ticker,
            "exit_price": exit_price,
            "reason":     reason,
            "pnl":        pnl,
        })

    for item in trailing_stop_updates:
        update_stop_loss(item["ticker"], item["new_stop_loss"])

    # Always snapshot so the chart has a live-price data point every intraday run
    portfolio.snapshot(current_prices, fx_rates=fx_rates, market_map=market_map)

    logger.info(
        f"run_intraday complete: closed={len(summary_closed)}, "
        f"trailing_updates={len(trailing_stop_updates)}"
    )
    if summary_closed:
        for t in summary_closed:
            pnl_val = t["pnl"]
            pnl_str = f"{'+' if pnl_val >= 0 else ''}${pnl_val:,.2f}"
            logger.info(
                f"  {t['ticker']:8s} SELL @ ${t['exit_price']:.2f}  "
                f"P&L: {pnl_str}  {t['reason']}"
            )


if __name__ == "__main__":
    """CLI entry point: python -m simulator.simulator [morning|evening]
    Defaults to 'morning'. Signals are always loaded from the most recent
    weekday scan via _last_scan_date_utc().
    """
    import sys
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "morning"
    if mode == "evening":
        run_evening()
    else:
        run_morning()
