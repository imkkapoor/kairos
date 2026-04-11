// ---------------------------------------------------------------------------
// API response types — matches backend/api.py exactly
// ---------------------------------------------------------------------------

// GET /api/dashboard → .portfolio
export interface PortfolioHistoryPoint {
  time: string;        // ISO 8601
  total_value: number;
}

export interface Position {
  qty: number;
  avg_cost: number;
  stop_loss: number | null;
  take_profit: number | null;
  strategy: string;
  opened_at: string;
  sector: string;
  currency: string;
  fx_rate: number | null;
  price: number | null;
  change_today: number | null;
  change_today_pct: number | null;
  [key: string]: unknown;
}

export interface PortfolioResponse {
  positions: Record<string, Position>;
  cash: number;
  total_value: number;
  currency: string;
  history: PortfolioHistoryPoint[];
}

// GET /api/dashboard → .metrics
export interface MetricBar {
  time: string;        // ISO 8601
  close: number;
}

export interface MetricData {
  current: number;
  change: number | null;
  change_pct: number | null;
  bars: MetricBar[];
}

export type MetricsResponse = Record<string, MetricData | null>;

// GET /api/dashboard
export interface DashboardResponse {
  portfolio: PortfolioResponse;
  metrics: MetricsResponse;
}

// GET /api/backtest
export interface BacktestRun {
  id: number;
  run_at: string;           // ISO 8601
  run_id: string | null;    // UUID — groups all windows from one make-backtest execution
  config_name: string;
  window_start: string;     // ISO date
  window_end: string;       // ISO date
  window_index: number;
  total_trades: number;
  win_rate: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  profit_factor: number | null;
  cagr: number | null;
  sharpe_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown: number | null;
  final_value_usd: number | null;
  total_pnl_usd: number | null;
  annualized_vol: number | null;
  currency: string;
  // Phase 4.5: vol filter fields
  use_vol_filter: boolean | null;
  avg_vix: number | null;
  pct_days_elevated: number | null;
  // Phase 4.7/4.8: circuit breaker, soft CB, crisis limits
  use_circuit_breaker: boolean | null;
  use_soft_cb: boolean | null;
  use_crisis_pos_limits: boolean | null;
  // Phase 4.6 VROC
  vroc_window: number | null;
  vroc_threshold: number | null;
  pct_days_spike: number | null;
  // Phase 4.7/4.8 CB detail
  pct_days_breaker_active: number | null;
  avg_cb_mult: number | null;
  pct_days_chatter_held: number | null;
  // ROOS category
  category: string | null;
  // Capital mode: capital_refresh or capital_compounded
  capital_mode: string | null;
  // Config origin and raw config
  config_origin: string | null;
  config: Record<string, unknown> | null;
}

export interface BacktestRunInfo {
  run_id: string;
  run_at: string;           // ISO 8601 — timestamp of first window inserted
  configs: string[];        // distinct config names in this run
  total_windows: number;
  total_trades: number | null;
  avg_sharpe: number | null;
  avg_cagr: number | null;
  avg_max_dd: number | null;
  avg_win_rate: number | null;
  total_pnl_usd: number | null;
  total_return_pct: number | null;  // total PnL / total invested capital across all windows
  currency: string;
  any_vol_filter: boolean;
  any_circuit_breaker: boolean;
  any_soft_cb: boolean;
  any_crisis_pos_limits: boolean;
  category: string | null;
  capital_mode: string | null;
  config_origin: string | null;
  avg_pnl_pct: number | null;  // avg per-window return (pnl / initial capital)
}

export interface BacktestRunListResponse {
  runs: BacktestRunInfo[];
}

export interface BacktestSummary {
  config_name: string;
  avg_sharpe: number | null;
  avg_calmar: number | null;
  avg_cagr: number | null;
  avg_max_dd: number | null;
  avg_win_rate: number | null;
  windows_tested: number;
  total_pnl: number | null;
  avg_pnl_pct: number | null;  // avg per-window return (pnl / initial capital)
}

export interface BacktestResponse {
  runs: BacktestRun[];
  summary: BacktestSummary[];
  run_list: BacktestRunInfo[];
}

// GET /api/ticker/{ticker}
export interface TickerBar {
  time: string;      // ISO 8601 UTC
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
}

export interface TickerChartResponse {
  ticker: string;
  range: string;
  bars: TickerBar[];
  trades: Trade[];
}

// GET /api/trades
export interface Trade {
  id: number;
  time: string;        // ISO 8601
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  fill_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  signal_strength: number | null;
  strategy: string;
  reason: string;
  signal_data: string | null;
  status: "filled" | "closed";
}

// GET /api/backtest/analytics (Phase 4.9)
export interface BacktestAnalytics {
  backtest_id: number;
  equity_curve: number[];
  drawdown_curve: number[];
  monthly_returns: Record<string, Record<string, number>>;
  regime_stats: Record<string, Record<string, number>>;
  timestamps: string[];
}

export interface BacktestAnalyticsResponse {
  analytics: BacktestAnalytics[];
}
