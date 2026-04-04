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
