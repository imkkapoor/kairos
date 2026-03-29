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
    sentiment_score    DOUBLE PRECISION   -- NULL until Phase 5
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
    currency        TEXT             NOT NULL DEFAULT 'USD',   -- Native currency of the asset
    fx_rate         DOUBLE PRECISION NOT NULL DEFAULT 1.0      -- FX rate to portfolio currency at fill time
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
