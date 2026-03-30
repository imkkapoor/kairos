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

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import (
    get_todays_signals,
    get_latest_indicators,
    get_todays_indicators,
    get_latest_close_prices,
    get_next_day_open_prices,
    close_trade,
    insert_trade,
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
    # 2. Load last night's signals, sorted by strength DESC
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
    todays_signals.sort(key=lambda s: float(s.get("strength", 0.0)), reverse=True)

    # ------------------------------------------------------------------
    # 3. Fetch live prices in ONE batch for signals + open positions
    # ------------------------------------------------------------------
    signal_tickers: set[str] = {s["ticker"] for s in todays_signals if s.get("ticker")}
    position_tickers: set[str] = set(portfolio.positions.keys())
    tickers_needed: list[str] = list(signal_tickers | position_tickers)

    logger.info(f"run_morning: fetching live prices for {len(tickers_needed)} tickers")
    current_prices = executor.fetch_live_prices(tickers_needed)

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
    # Determine which foreign currencies are needed
    needed_ccys = {executor.get_ticker_currency(t, market_map) for t in tickers_needed}
    for ccy in needed_ccys:
        if ccy != portfolio_ccy and ccy not in fx_rates:
            rate = executor.fetch_fx_rate(ccy, portfolio_ccy)
            if rate is not None:
                fx_rates[ccy] = rate
            else:
                logger.warning(f"run_morning: could not fetch {ccy}→{portfolio_ccy} rate, using 1.0")
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
                currency        = trade.get("currency", "USD"),
                fx_rate         = trade.get("fx_rate", 1.0),
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
            close_trade(trade["ticker"], trade["fill_price"], trade["reason"])
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
    needed_ccys = {executor.get_ticker_currency(t, market_map) for t in position_tickers}
    for ccy in needed_ccys:
        if ccy != portfolio_ccy and ccy not in fx_rates:
            rate = executor.fetch_fx_rate(ccy, portfolio_ccy)
            if rate is not None:
                fx_rates[ccy] = rate
            else:
                logger.warning(f"run_evening: could not fetch {ccy}→{portfolio_ccy} rate, using 1.0")
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


def run_morning_replay(signal_date_str: str, commit: bool = False) -> None:
    """Replay morning execution for a past signal date.

    Loads signals stored in the DB for signal_date_str, fills at the next
    trading day's opening price from price_data.

    Parameters
    ----------
    signal_date_str : str
        The date whose signals to replay, in YYYY-MM-DD format (e.g. 2026-03-26).
    commit : bool
        If False (default), dry-run only — no DB writes.
        If True, writes trades and portfolio snapshot using the historical fill
        date as the timestamp, exactly as run_morning() would for a live day.
    """
    try:
        signal_dt = datetime.strptime(signal_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"ERROR: DATE must be YYYY-MM-DD, got: {signal_date_str!r}")
        return

    mode_label = "COMMIT" if commit else "DRY RUN"
    print()
    print(f"[{mode_label}] Replaying signals from {signal_date_str}")
    if not commit:
        print("No DB writes — live portfolio is NOT modified.")
    else:
        print("Will write trades + snapshot to DB at historical fill date.")
    print("=" * 52)

    logger.info(f"run_morning_replay: signal date={signal_date_str}")

    # ------------------------------------------------------------------
    # 1. Load signals for that date (read-only)
    # ------------------------------------------------------------------
    signals_df = get_todays_signals(for_date=signal_dt)
    if signals_df.empty:
        print(f"No signals found in DB for {signal_date_str}.")
        print("Make sure 'make scan' ran on that day.")
        return

    todays_signals: list[dict] = signals_df.to_dict("records")
    todays_signals.sort(key=lambda s: float(s.get("strength", 0.0)), reverse=True)
    signal_tickers: set[str] = {s["ticker"] for s in todays_signals if s.get("ticker")}

    logger.info(f"run_morning_replay: {len(todays_signals)} signals for {len(signal_tickers)} tickers")

    # ------------------------------------------------------------------
    # 2. Fetch next trading day's open prices from price_data DB (read-only)
    # ------------------------------------------------------------------
    fill_prices, fill_date = get_next_day_open_prices(list(signal_tickers), signal_dt)

    if not fill_prices:
        print(
            f"No price data in DB for the day after {signal_date_str}.\n"
            f"Run 'make fetch' to ensure price_data is populated."
        )
        return

    logger.info(f"run_morning_replay: fill date={fill_date}, {len(fill_prices)} prices available")

    # ------------------------------------------------------------------
    # 3. Load indicators for signal_date from DB (read-only)
    # ------------------------------------------------------------------
    tickers_list = list(signal_tickers)
    today_indicators = get_todays_indicators(tickers_list, for_date=signal_dt)
    if not today_indicators:
        # indicators are timestamped at price-bar time (e.g. 04:00 UTC), not midnight;
        # fall back to latest available row per ticker
        today_indicators = get_latest_indicators(tickers_list)
        logger.info("run_morning_replay: fell back to get_latest_indicators (no exact date match)")

    # ------------------------------------------------------------------
    # 4. Load current portfolio state (read-only — snapshot never called)
    # ------------------------------------------------------------------
    portfolio = Portfolio.load_from_db()

    # ------------------------------------------------------------------
    # 5. Sector/market maps + FX rates
    # ------------------------------------------------------------------
    sector_map   = get_sector_map()
    market_map   = get_market_map()
    portfolio_ccy = portfolio.currency
    fx_rates: dict[str, float] = {portfolio_ccy: 1.0}
    needed_ccys = {executor.get_ticker_currency(t, market_map) for t in signal_tickers}
    for ccy in needed_ccys:
        if ccy != portfolio_ccy and ccy not in fx_rates:
            rate = executor.fetch_fx_rate(ccy, portfolio_ccy)
            fx_rates[ccy] = rate if rate is not None else 1.0

    # ------------------------------------------------------------------
    # 6. Execute signals against fill_prices (portfolio mutated in memory,
    #    but nothing is persisted — no insert_trade / snapshot / mark_acted_on)
    # ------------------------------------------------------------------
    already_held = set(portfolio.positions.keys())
    eligible_signals = [
        s for s in todays_signals
        if s.get("signal_type", "").lower() != "buy" or s.get("ticker") not in already_held
    ]

    executed, skipped = executor.execute_signals(
        eligible_signals, portfolio, fill_prices, today_indicators, sector_map,
        market_map=market_map, fx_rates=fx_rates,
    )

    # ------------------------------------------------------------------
    # 7. Optionally persist to DB (commit mode)
    # ------------------------------------------------------------------
    # Use 9:31 AM ET on the fill date as the canonical trade timestamp
    fill_datetime = datetime(
        int(fill_date[0:4]), int(fill_date[5:7]), int(fill_date[8:10]),
        14, 31, 0, tzinfo=timezone.utc,  # 09:31 ET = 14:31 UTC (EST)
    )

    if commit:
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
                    currency        = trade.get("currency", "USD"),
                    fx_rate         = trade.get("fx_rate", 1.0),
                    trade_time      = fill_datetime,
                )
                signal_id = trade.get("signal_id")
                if signal_id is not None:
                    try:
                        mark_signal_acted_on(int(signal_id))
                    except Exception as exc:
                        logger.warning(f"mark_signal_acted_on({signal_id}): {exc}")
            elif trade["side"] == "sell":
                close_trade(trade["ticker"], trade["fill_price"], trade["reason"])
                signal_id = trade.get("signal_id")
                if signal_id is not None:
                    try:
                        mark_signal_acted_on(int(signal_id))
                    except Exception as exc:
                        logger.warning(f"mark_signal_acted_on({signal_id}): {exc}")

        portfolio.snapshot(fill_prices, fx_rates=fx_rates, market_map=market_map,
                           snapshot_time=fill_datetime)
        logger.info(f"run_morning_replay: committed {len(executed)} trades to DB at {fill_datetime}")

    # ------------------------------------------------------------------
    # 8. Print summary
    # ------------------------------------------------------------------
    total_value = portfolio.get_total_value(fill_prices, fx_rates, market_map)
    opened = [t for t in executed if t["side"] == "buy"]
    sold   = [t for t in executed if t["side"] == "sell"]
    verb_open  = "Opened" if commit else "Would open"
    verb_close = "Closed" if commit else "Would close"

    print(f"\n-- Replay: signals {signal_date_str}  →  fills at {fill_date} open --")
    print(f"{verb_open} ({len(opened)}):")
    if opened:
        for t in opened:
            ccy_label = t.get('currency', '')
            fx_label  = f" fx={t['fx_rate']:.4f}" if t.get('fx_rate', 1.0) != 1.0 else ""
            print(
                f"  {t['ticker']:8s} BUY  {t['quantity']:.0f}sh @ ${t['fill_price']:.2f} {ccy_label}{fx_label}  "
                f"SL:${t['stop_loss']:.2f}  TP:${t['take_profit']:.2f}  str:{t['signal_strength']:.2f}"
            )
    else:
        print("  (none)")
    print(f"{verb_close} ({len(sold)}):")
    if sold:
        for t in sold:
            print(f"  {t['ticker']:8s} SELL @ ${t['fill_price']:.2f}  {t.get('reason', '')}")
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
        f"Portfolio: ${total_value:,.2f} {portfolio.currency}  |"
        f"  Cash: ${portfolio.cash:,.2f} {portfolio.currency}  |"
        f"  Open: {len(portfolio.positions)}/{portfolio.MAX_OPEN_POSITIONS}"
    )
    if fx_rates:
        fx_parts = [f"{c}={r:.4f}" for c, r in fx_rates.items() if c != portfolio.currency]
        if fx_parts:
            print(f"FX rates: {', '.join(fx_parts)}")
    print("=" * 52)
    if commit:
        print(f"[COMMIT] {len(opened)} trades written to DB, snapshot saved at {fill_date} 09:31 ET.")
    else:
        print("[DRY RUN] Nothing was written to the database.")
    logger.info(
        f"run_morning_replay complete (commit={commit}): opened={len(opened)}, closed={len(sold)}, skipped={len(skipped)}"
    )


if __name__ == "__main__":
    """CLI entry point: python -m simulator.simulator [morning|evening|replay DATE]
    Defaults to 'morning'. Pass 'replay YYYY-MM-DD' to do a dry-run replay.
    """
    import sys
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "morning"
    if mode == "evening":
        run_evening()
    elif mode == "replay":
        if len(sys.argv) < 3:
            print("Usage: python -m simulator.simulator replay YYYY-MM-DD [--commit]")
            sys.exit(1)
        run_morning_replay(sys.argv[2], commit="--commit" in sys.argv)
    else:
        run_morning()
