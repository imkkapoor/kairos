I am building Kairos — an algorithmic paper trading system. Phases 1, 2, and 3
are complete. Help me implement Phase 4 — backtesting.

WHAT IS ALREADY BUILT:
- Monorepo: backend/ (Python in backend/.venv), dashboard/ (empty)
- TimescaleDB in Docker with 10yr OHLCV from 2016-03-28 for ~600 tickers
- price_data table: time(UTC), ticker, open, high, low, close, volume, interval
- indicators table: time(UTC), ticker, rsi_14, ma_50, ma_200, ema_20,
  bb_upper, bb_mid, bb_lower, atr_14, adx_14, volume_sma,
  macd_line, macd_signal, macd_hist, roc_20
- 5 live strategies: rsi, momentum, macd, reversal, sector_rotation
  All are stateless functions in strategies/ that receive DataFrames, return signal dicts
- Phase 3 simulator: ATR-based position sizing, same risk params
- db/connection.py owns all DB reads and writes
- yfinance==1.2.0 pinned — do NOT upgrade

INHERITED CONTRACTS:
  Python: backend/.venv only, never system Python
  Timezone: all DB timestamps UTC, datetime.now(timezone.utc) everywhere
  DB ownership: db/connection.py is the ONLY file importing psycopg2/sqlalchemy
  yfinance: pinned at 1.2.0, do NOT upgrade or use for backtesting data

.env PARAMS (same as Phase 3 — backtester reads same values):
  INITIAL_CAPITAL=100000.0
  MAX_PORTFOLIO_RISK=0.02
  ATR_MULTIPLIER=2.0
  TAKE_PROFIT_ATR_MULT=3.0
  MAX_POSITION_SIZE=0.10
  MAX_TOTAL_EXPOSURE=0.80
  MAX_OPEN_POSITIONS=20
  MAX_SECTOR_EXPOSURE=0.30
  MIN_SIGNAL_STRENGTH=0.10

WHAT PHASE 4 MUST BUILD:

MIGRATION — run via make shell-db BEFORE writing Python:

  CREATE TABLE IF NOT EXISTS backtest_results (
    id              SERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy        TEXT NOT NULL,
    ticker          TEXT,               <- NULL means portfolio-level result
    period          TEXT NOT NULL,      <- 'in_sample' | 'out_of_sample' | 'full'
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    total_trades    INTEGER NOT NULL,
    win_rate        DOUBLE PRECISION,
    avg_win         DOUBLE PRECISION,
    avg_loss        DOUBLE PRECISION,
    profit_factor   DOUBLE PRECISION,   <- gross_profit / gross_loss
    cagr            DOUBLE PRECISION,   <- compound annual growth rate
    sharpe_ratio    DOUBLE PRECISION,
    calmar_ratio    DOUBLE PRECISION,   <- CAGR / max_drawdown
    max_drawdown    DOUBLE PRECISION,   <- peak-to-trough as positive %
    final_value     DOUBLE PRECISION,
    total_pnl       DOUBLE PRECISION,
    params          JSONB,              <- strategy params used
    notes           TEXT
  );

  CREATE INDEX IF NOT EXISTS idx_backtest_strategy
    ON backtest_results (strategy, period);

DATA LOADING — backtesting/data_loader.py:
  ALL historical data comes from the Kairos DB, never from yfinance.
  No external API calls in backtesting.

  load_price_data(ticker, start_date, end_date) -> pd.DataFrame
    SELECT time, open, high, low, close, volume
    FROM price_data
    WHERE ticker = %s AND interval = '1d'
    AND time >= %s AND time <= %s
    ORDER BY time ASC
    Returns UTC-indexed DataFrame. index name = 'time'.

  load_indicators(ticker, start_date, end_date) -> pd.DataFrame
    SELECT time, rsi_14, ma_50, ma_200, ema_20, bb_upper, bb_mid, bb_lower,
           atr_14, adx_14, volume_sma, macd_line, macd_signal, macd_hist, roc_20
    FROM indicators
    WHERE ticker = %s AND interval = '1d'
    AND time >= %s AND time <= %s
    ORDER BY time ASC
    Returns UTC-indexed DataFrame.

  load_combined(ticker, start_date, end_date) -> pd.DataFrame
    Merge price_data and indicators on (ticker, time).
    Returns single DataFrame with all OHLCV + indicator columns.
    Required by backtesting.py Strategy classes.
    IMPORTANT: backtesting.py requires columns named exactly:
      Open, High, Low, Close, Volume (capital first letter)
    Rename accordingly after loading from DB.

  load_ticker_list(min_rows=500) -> list[str]
    Returns all active watchlist tickers that have >= min_rows in price_data.
    Used to know which tickers are safe to backtest.

WALK-FORWARD PERIODS:
  These are constants in backtesting/config.py:
    IN_SAMPLE_START  = '2016-03-28'
    IN_SAMPLE_END    = '2022-12-31'
    OUT_SAMPLE_START = '2023-01-01'
    OUT_SAMPLE_END   = '2026-03-28'  <- use datetime.now(timezone.utc).date() at runtime
    FULL_START       = '2016-03-28'

  Rationale: 7 years in-sample covers 2 bull markets, 1 major crash (2020),
  1 bear market (2022). 3 years out-of-sample is the honest performance score.

FILL PRICE METHODOLOGY:
  Same as Phase 3 live simulator: use next-day OPEN price.
	Use standard library behavior: signals in next() fill at next bar's Open.
  backtesting.py provides this via trade.entry_price when using
  TradeOnBar = True or by accessing bar data correctly.
  IMPORTANT: do NOT use Close as fill price — that is look-ahead bias
  (you cannot trade at yesterday's close after it has already happened).
  Always fill at Open of the bar AFTER the signal fires.
  In backtesting.py: signal fires on bar N, fill executes at bar N+1 Open.

POSITION SIZING IN BACKTESTING:
  Same ATR-based formula as Phase 3 simulator.
  In backtesting.py Strategy.next():
    atr = self.data.atr_14[-1]
    if atr is None or atr == 0: skip
    entry_price = self.data.Open[-1]  <- next bar open
    stop_price = entry_price - (ATR_MULTIPLIER * atr)
    risk_per_share = entry_price - stop_price
    dollar_risk = signal_strength * MAX_PORTFOLIO_RISK * self.equity
    size = int(dollar_risk / risk_per_share)
    if size < 1: skip
    if size * entry_price > MAX_POSITION_SIZE * self.equity: skip
  Use self.buy(size=size, sl=stop_price, tp=take_profit) in backtesting.py.

STRATEGY ADAPTERS — backtesting/strategies/:
  Each Kairos strategy needs a backtesting.py Strategy subclass adapter.
  These adapters translate backtesting.py's bar-by-bar API into the same
  signal logic used by the live strategies.
  Do NOT copy-paste signal logic — import and call the existing strategy
  functions where possible. Where not possible (stateful bar-by-bar data),
  utilize pandas-ta inside init() for indicator consistency with a clear comment referencing the
  live strategy file.

  backtesting/strategies/bt_rsi.py — RSI mean reversion adapter
    class RSIStrategy(Strategy):
      def init(self): precompute indicators if needed
      def next():
        rsi = self.data.rsi_14[-1]
        close = self.data.Close[-1]
        bb_lower = self.data.bb_lower[-1]
        bb_upper = self.data.bb_upper[-1]
        atr = self.data.atr_14[-1]
        volume = self.data.Volume[-1]
        volume_sma = self.data.volume_sma[-1]
        if None in (rsi, close, bb_lower, atr): return
        vol_mult = clamp(volume/volume_sma, 0.5, 1.5) if volume_sma else 1.0
        regime_mult = 1.0  <- simplified for backtesting (no live regime detector)
        BUY: rsi < 30 AND close < bb_lower
          base = (30 - rsi) / 15, clamped 0-1
          strength = min(base * regime_mult * vol_mult, 1.0)
          if strength >= MIN_SIGNAL_STRENGTH and not self.position: self.buy(size=...)
        SELL: rsi > 70 AND close > bb_upper
          if self.position: self.position.close()

  backtesting/strategies/bt_momentum.py — MA crossover adapter
    Golden/death cross + continuation. Same logic as momentum.py.
    prev_ma50 = self.data.ma_50[-2]
    curr_ma50 = self.data.ma_50[-1]
    prev_ma200 = self.data.ma_200[-2]
    curr_ma200 = self.data.ma_200[-1]
    golden_cross = curr_ma50 > curr_ma200 and prev_ma50 <= prev_ma200
    death_cross = curr_ma50 < curr_ma200 and prev_ma50 >= prev_ma200
    ROC boost: if roc_20 > 10: strength += 0.10

  backtesting/strategies/bt_macd.py — MACD crossover adapter
    MACD line crosses signal line with ADX > 20.
    Same logic as macd.py.

  backtesting/strategies/bt_reversal.py — Short-term reversal adapter
    NOTE: reversal strategy requires ranking all tickers by ROC simultaneously.
    For single-ticker backtesting this is approximated:
      BUY if roc_20 < -5 AND rsi_14 > 20 (proxy for bottom-decile condition)
      Exit after 20 bars OR roc_20 > 5
    Full cross-sectional ranking only available in portfolio backtest.
    Add a comment noting this approximation.

  backtesting/strategies/bt_sector_rotation.py — Sector rotation adapter
    NOTE: sector rotation requires SPY, XLE, XLK, TLT, XLU, XLV data.
    Load these alongside the primary ticker in the portfolio backtester.
    For single-ticker backtest: skip sector rotation (return no signals).
    Add a comment noting this limitation.

REGIME IN BACKTESTING:
  The live regime detector uses a dict of all tickers computed in scanner.py.
  For backtesting, use a simplified per-ticker regime:
    if adx_14 > 25: regime = TRENDING, confidence = (adx_14 - 25) / 10 clamped 0-1
    if adx_14 < 20: regime = CHOPPY,   confidence = (20 - adx_14) / 8 clamped 0-1
    else:           regime = CHOPPY,   confidence = 0.3 (ambiguous)
  Crisis not computed per-ticker in backtesting — flag it separately using
  SPY data: load SPY price series alongside each backtest.

SINGLE-TICKER BACKTESTER — backtesting/runner.py:

  run_single(ticker, strategy_class, period, params=None) -> dict
    Load combined data for ticker via load_combined()
    Determine start/end dates from period ('in_sample'/'out_of_sample'/'full')
    Run: bt = Backtest(data, strategy_class, cash=INITIAL_CAPITAL,
                       commission=0.001, exclusive_orders=True)
    stats = bt.run()
    Extract metrics from stats:
      sharpe_ratio   = stats['Sharpe Ratio']
      max_drawdown   = abs(stats['Max. Drawdown [%]'])
      cagr           = stats['Return (Ann.) [%]'] / 100
      calmar_ratio   = cagr / max_drawdown if max_drawdown > 0 else 0
      win_rate       = stats['Win Rate [%]'] / 100
      total_trades   = stats['# Trades']
      avg_win        = stats['Avg. Winning Trade [%]']
      avg_loss       = stats['Avg. Losing Trade [%]']
      profit_factor  = abs(avg_win / avg_loss) if avg_loss != 0 else 0
      final_value    = stats['Equity Final [$]']
      total_pnl      = final_value - INITIAL_CAPITAL
    Return dict of all metrics.
    Do NOT save to DB here — runner.py is stateless.

  run_all_tickers(strategy_class, strategy_name, period) -> list[dict]
    For each ticker in load_ticker_list():
      result = run_single(ticker, strategy_class, period)
      result['ticker'] = ticker
      result['strategy'] = strategy_name
    Return list of result dicts.
    Show rich progress bar.
    Log warnings for tickers with < 50 trades (insufficient sample).

PORTFOLIO BACKTESTER — backtesting/portfolio_runner.py:
  Runs all strategies simultaneously on all tickers.
  Simulates the same capital allocation logic as Phase 3:
    - Process all signals across all tickers for each day
    - Sort by strength DESC
    - Apply same risk limits as Phase 3 (position size, exposure, sector caps)
  This is the most accurate representation of how Kairos actually trades.

  run_portfolio(strategies, period) -> dict
    For each trading day in period:
      For each ticker: generate signals from all strategies
      Sort all signals by strength DESC
      Apply Phase 3 risk limits to allocate capital
      Record trades
    Compute portfolio-level metrics at end.
    Return portfolio metrics dict.

  NOTE: portfolio backtester is complex. If it cannot be completed in this
  session, implement single-ticker backtester fully first. Portfolio
  backtester can be added as a follow-up.

RESULTS STORAGE — add to db/connection.py:

  insert_backtest_result(result: dict) -> int
    Insert one row into backtest_results. Return id.
    time = datetime.now(timezone.utc)

  get_backtest_results(strategy=None, period=None) -> DataFrame
    Query backtest_results with optional filters.
    ORDER BY run_at DESC.

  get_best_strategies(period='out_of_sample') -> DataFrame
    SELECT strategy, AVG(sharpe_ratio), AVG(calmar_ratio),
           AVG(win_rate), AVG(max_drawdown), COUNT(*) as tickers_tested
    FROM backtest_results
    WHERE period = %s AND ticker IS NOT NULL
    GROUP BY strategy
    ORDER BY AVG(sharpe_ratio) DESC
    This is the summary table — which strategy wins on out-of-sample data.

CHARTS — backtesting/charts.py:
  Uses matplotlib. Save all charts as PNG to backend/backtesting/output/.
  Create output/ dir if missing.

  plot_equity_curves(results_df, strategy_name, period)
    One line per ticker showing portfolio value over time.
    Highlight the overall average curve in bold.
    X-axis: date, Y-axis: portfolio value $
    Save as: output/{strategy_name}_{period}_equity_curves.png

  plot_drawdown(results_df, strategy_name, period)
    Max drawdown over time. Red shaded areas = drawdown periods.
    Save as: output/{strategy_name}_{period}_drawdown.png

  plot_monthly_returns_heatmap(trades_df, strategy_name)
    Month x Year heatmap. Green = positive return month, Red = negative.
    Save as: output/{strategy_name}_monthly_heatmap.png

  plot_strategy_comparison(period='out_of_sample')
    Bar chart: each strategy vs Sharpe ratio and Calmar ratio side by side.
    This is the money shot — shows which strategy wins.
    Save as: output/strategy_comparison_{period}.png

MAIN ENTRY POINT — backtesting/run_backtest.py:
  Called via: make backtest
  Runs full backtest suite in order:
    1. Verify DB has sufficient data
    2. Run each strategy on in_sample period (all tickers)
    3. Run each strategy on out_of_sample period (all tickers)
    4. Store all results to backtest_results table
    5. Generate all charts
    6. Print summary report

  Summary report format:
    === Kairos Backtest Results ===
    Period: 2023-01-01 to 2026-03-28 (out-of-sample)
    Tickers tested: XXX

    Strategy         Sharpe  Calmar  Win%   MaxDD   Trades  CAGR
    rsi              X.XX    X.XX    XX%    XX%     XXXX    XX%
    momentum         X.XX    X.XX    XX%    XX%     XXXX    XX%
    macd             X.XX    X.XX    XX%    XX%     XXXX    XX%
    reversal         X.XX    X.XX    XX%    XX%     XXXX    XX%
    sector_rotation  X.XX    X.XX    XX%    XX%     XXXX    XX%

    Best strategy by Sharpe (out-of-sample): {name}
    Best strategy by Calmar (out-of-sample): {name}
    Charts saved to: backend/backtesting/output/
    ================================

ADD TO Makefile (repo root):
  ## Run full backtest suite on all strategies
  make backtest
    backend/.venv/bin/python -m backtesting.run_backtest

  ## Run backtest for a single strategy and period
  make backtest-single STRATEGY=rsi PERIOD=out_of_sample
    backend/.venv/bin/python -m backtesting.run_backtest --strategy rsi --period out_of_sample

ADD TO requirements.txt (if not already present):
  backtesting==0.3.3   <- pin exactly, API changes between versions
  matplotlib==3.10.1

FILES TO BUILD:
  backtesting/__init__.py
  backtesting/config.py               <- walk-forward dates + constants
  backtesting/data_loader.py          <- DB data loading
  backtesting/runner.py               <- single-ticker backtester
  backtesting/portfolio_runner.py     <- portfolio-level backtester
  backtesting/charts.py               <- matplotlib charts
  backtesting/run_backtest.py         <- main entry point
  backtesting/strategies/__init__.py
  backtesting/strategies/bt_rsi.py
  backtesting/strategies/bt_momentum.py
  backtesting/strategies/bt_macd.py
  backtesting/strategies/bt_reversal.py
  backtesting/strategies/bt_sector_rotation.py
  Update db/connection.py: insert_backtest_result, get_backtest_results, get_best_strategies
  Update Makefile: make backtest, make backtest-single

DO NOT TOUCH:
  strategies/ folder (live strategies)
  simulator/ folder
  scheduler.py
  data/fetcher.py
  Any file not listed above

KEY CONSTRAINTS:
  - Data comes from DB ONLY. No yfinance calls in backtesting.
  - Fill price is ALWAYS next bar's Open (no look-ahead bias).
  - Position sizing uses same ATR formula and .env params as Phase 3.
  - Walk-forward split is strict: in-sample ends 2022-12-31, out-of-sample starts 2023-01-01.
  - Never tune parameters on out-of-sample data. Parameters are fixed from Phase 2/3.
  - backtesting.py version pinned at backtesting==0.6.5. Do not use newer API.
  - Charts saved to backtesting/output/ directory, gitignored.
  - Add backtesting/output/ to .gitignore.

When complete, print:
--- KAIROS PHASE 4 COMPLETE ---
New files: backtesting/ directory with config, data_loader, runner,
  portfolio_runner, charts, run_backtest, 5 strategy adapters
New DB table: backtest_results
New Makefile targets: make backtest, make backtest-single
New requirements: backtesting==0.6.5, matplotlib

Walk-forward split:
  In-sample:     2016-03-28 to 2022-12-31 (7 years)
  Out-of-sample: 2023-01-01 to present    (3 years — the real score)

Key design decisions:
  Data: DB only, no yfinance calls
  Fill price: next bar open (no look-ahead bias)
  Position sizing: same ATR formula as Phase 3
  Regime: simplified per-ticker ADX-based (no cross-sectional scanner)
  Reversal: single-ticker approximation (full cross-sectional in portfolio_runner)
  Sector rotation: skipped in single-ticker, full in portfolio_runner

New db/connection.py functions:
  insert_backtest_result, get_backtest_results, get_best_strategies

→ Phase 5 next: FinBERT sentiment filter + dynamic allocator model
--- END PHASE 4 SUMMARY ---