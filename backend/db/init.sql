-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- price_data: OHLCV bars (hypertable partitioned on time)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_data (
    time     TIMESTAMPTZ      NOT NULL,
    ticker   TEXT             NOT NULL,
    open     DOUBLE PRECISION NOT NULL,
    high     DOUBLE PRECISION NOT NULL,
    low      DOUBLE PRECISION NOT NULL,
    close    DOUBLE PRECISION NOT NULL,
    volume   BIGINT           NOT NULL,
    interval TEXT             NOT NULL DEFAULT '1d',
    source   TEXT             NOT NULL DEFAULT 'yfinance',
    UNIQUE (time, ticker, interval)
);

SELECT create_hypertable('price_data', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_price_data_ticker_time
    ON price_data (ticker, time DESC);

-- ---------------------------------------------------------------------------
-- indicators: computed technical indicators (hypertable partitioned on time)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS indicators (
    time       TIMESTAMPTZ      NOT NULL,
    ticker     TEXT             NOT NULL,
    interval   TEXT             NOT NULL DEFAULT '1d',
    rsi_14     DOUBLE PRECISION,
    ma_50      DOUBLE PRECISION,
    ma_200     DOUBLE PRECISION,
    ema_20     DOUBLE PRECISION,
    bb_upper   DOUBLE PRECISION,
    bb_mid     DOUBLE PRECISION,
    bb_lower   DOUBLE PRECISION,
    atr_14     DOUBLE PRECISION,   -- Required by Phase 3 position sizing
    adx_14     DOUBLE PRECISION,   -- Required by Phase 2 regime detection
    volume_sma DOUBLE PRECISION,
    macd_line  DOUBLE PRECISION,   -- MACD line (Phase 2)
    macd_signal DOUBLE PRECISION,  -- MACD signal line (Phase 2)
    macd_hist  DOUBLE PRECISION,   -- MACD histogram (Phase 2)
    roc_20     DOUBLE PRECISION,   -- Rate of change 20-period (Phase 2)
    UNIQUE (time, ticker, interval)
);

SELECT create_hypertable('indicators', 'time', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- signals: strategy-generated trade signals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id                 SERIAL PRIMARY KEY,
    time               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker             TEXT,
    strategy           TEXT,
    signal_type        TEXT CHECK (signal_type IN ('buy', 'sell', 'hold')),
    strength           DOUBLE PRECISION,
    reason             TEXT NOT NULL,
    indicator_vals     JSONB,
    regime             TEXT,
    regime_confidence  DOUBLE PRECISION,
    acted_on           BOOLEAN     NOT NULL DEFAULT FALSE,
    sentiment_score    DOUBLE PRECISION,  -- NULL until Phase 5
    z_score            DOUBLE PRECISION   -- Cross-universe normalized score
);

-- ---------------------------------------------------------------------------
-- trades: simulated paper trade executions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    time            TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    ticker          TEXT             NOT NULL,
    side            TEXT             CHECK (side IN ('buy', 'sell')),
    quantity        DOUBLE PRECISION NOT NULL,
    fill_price      DOUBLE PRECISION NOT NULL,
    stop_loss       DOUBLE PRECISION,          -- Required by Phase 3
    take_profit     DOUBLE PRECISION,          -- Required by Phase 3
    signal_strength DOUBLE PRECISION,          -- Required by Phase 3 portfolio recovery
    strategy        TEXT             NOT NULL,
    reason          TEXT             NOT NULL,
    signal_data     JSONB,
    status          TEXT             NOT NULL DEFAULT 'filled',
    fill_type       TEXT             -- 'Normal Fill' | 'Capped to Max Size' | 'Partial Fill' | 'Capped & Partial'
);

-- ---------------------------------------------------------------------------
-- portfolio_snapshots: point-in-time portfolio state (hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    time        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    cash        DOUBLE PRECISION NOT NULL,
    total_value DOUBLE PRECISION NOT NULL,
    positions   JSONB            NOT NULL,  -- Full position context for Phase 3
    daily_pnl   DOUBLE PRECISION,
    total_pnl   DOUBLE PRECISION,
    drawdown    DOUBLE PRECISION,
    currency    TEXT             NOT NULL DEFAULT 'CAD'  -- Portfolio base currency
);

SELECT create_hypertable('portfolio_snapshots', 'time', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- watchlist: universe of tracked tickers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist (
    ticker   TEXT        PRIMARY KEY,
    name     TEXT,
    sector   TEXT,
    market   TEXT        NOT NULL DEFAULT 'US',
    active   BOOLEAN     NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes    TEXT
);

-- ---------------------------------------------------------------------------
-- fx_rates: historical FX rates at the 9:31 AM ET bar (hypertable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fx_rates (
    time          TIMESTAMPTZ NOT NULL,
    pair          TEXT NOT NULL,           -- e.g. 'USDCAD' (1 USD = X CAD)
    rate          DOUBLE PRECISION NOT NULL,
    source        TEXT NOT NULL DEFAULT 'yfinance',
    CONSTRAINT fx_rates_unique UNIQUE (time, pair)
);
SELECT create_hypertable('fx_rates', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_fx_rates_pair ON fx_rates (pair, time DESC);

-- ---------------------------------------------------------------------------
-- fetch_log: one row per fetch run summarising what happened
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_log (
    id              SERIAL PRIMARY KEY,
    tickers_total   INTEGER NOT NULL,
    tickers_success INTEGER NOT NULL,
    tickers_skipped INTEGER NOT NULL,
    tickers_failed  INTEGER NOT NULL,
    failed_tickers  TEXT,
    rows_inserted   INTEGER NOT NULL,
    duration_secs   DOUBLE PRECISION,
    notes           TEXT,
    fetch_time      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- backtest_results: rolling out-of-sample (ROOS) window results (Phase 4)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_results (
    id              SERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    run_id          UUID,                         -- groups all windows from one make-backtest execution
    config_name     TEXT             NOT NULL,
    config          JSONB,
    window_start    DATE             NOT NULL,
    window_end      DATE             NOT NULL,
    window_index    INTEGER          NOT NULL,
    total_trades    INTEGER,
    win_rate        DOUBLE PRECISION,
    avg_win_pct     DOUBLE PRECISION,
    avg_loss_pct    DOUBLE PRECISION,
    profit_factor   DOUBLE PRECISION,
    cagr            DOUBLE PRECISION,
    sharpe_ratio    DOUBLE PRECISION,
    calmar_ratio    DOUBLE PRECISION,
    max_drawdown    DOUBLE PRECISION,
    final_value_usd DOUBLE PRECISION,
    total_pnl_usd   DOUBLE PRECISION,
    annualized_vol  DOUBLE PRECISION,
    currency        TEXT             NOT NULL DEFAULT 'USD',
    notes           TEXT,
    -- Vol filter (Phase 4.5)
    use_vol_filter      BOOLEAN          DEFAULT FALSE,
    avg_vix             DOUBLE PRECISION,
    pct_days_elevated   DOUBLE PRECISION,
    -- VROC spike (Phase 4.6)
    vroc_window         INTEGER          DEFAULT 10,
    vroc_threshold      DOUBLE PRECISION DEFAULT 0.20,
    pct_days_spike      DOUBLE PRECISION,
    -- Circuit breaker (Phase 4.7)
    use_circuit_breaker     BOOLEAN          DEFAULT FALSE,
    dd_trigger              DOUBLE PRECISION DEFAULT 0.15,
    dd_reset                DOUBLE PRECISION DEFAULT 0.10,
    pct_days_breaker_active DOUBLE PRECISION,
    -- Soft circuit breaker (Phase 4.8)
    use_soft_cb             BOOLEAN          DEFAULT FALSE,
    cb_soft_start           DOUBLE PRECISION,
    cb_hard_stop            DOUBLE PRECISION,
    cb_min_mult             DOUBLE PRECISION,
    avg_cb_mult             DOUBLE PRECISION,
    pct_days_chatter_held   DOUBLE PRECISION,
    use_crisis_pos_limits   BOOLEAN          DEFAULT FALSE,
    crisis_max_positions    INTEGER,
    min_dollar_risk         DOUBLE PRECISION,
    pct_signals_below_floor DOUBLE PRECISION,
    -- ROOS metadata
    category        TEXT             DEFAULT 'roos',          -- always 'roos' (Rolling Out-of-Sample)
    capital_mode    TEXT             DEFAULT 'capital_refresh', -- capital_refresh | capital_compounded
    config_origin   TEXT             DEFAULT 'manual'          -- manual (hand-crafted) | predicted (ML-optimised)
);

-- ---------------------------------------------------------------------------
-- backtest_analytics: pre-computed chart data for backtest visualisation (Phase 4.9)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_analytics (
    id              SERIAL PRIMARY KEY,
    backtest_id     INTEGER          NOT NULL REFERENCES backtest_results(id) ON DELETE CASCADE,
    equity_curve    JSONB,           -- Array of daily portfolio values
    drawdown_curve  JSONB,           -- Array of daily drawdown percentages
    monthly_returns JSONB,           -- Nested: Year -> Month -> return value
    regime_stats    JSONB,           -- Nested: Strategy -> Regime -> net PnL
    timestamps      JSONB,           -- Array of ISO date strings (aligns with curves)
    UNIQUE(backtest_id)
);

-- ---------------------------------------------------------------------------
-- vix_data: daily VIX close prices for volatility regime filtering (Phase 4.5)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vix_data (
    time   TIMESTAMPTZ      NOT NULL,
    close  DOUBLE PRECISION NOT NULL,
    source TEXT             NOT NULL DEFAULT 'yfinance',
    CONSTRAINT vix_data_unique UNIQUE (time)
);

SELECT create_hypertable('vix_data', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_vix_data_time ON vix_data (time DESC);
