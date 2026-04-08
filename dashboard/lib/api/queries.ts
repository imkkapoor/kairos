import { useQuery } from "@tanstack/react-query";

import apiClient from "./client";
import type {
  DashboardResponse,
  Trade,
  BacktestResponse,
  BacktestRunListResponse,
  TickerChartResponse,
} from "../types/api";

// ---------------------------------------------------------------------------
// Query keys — single source of truth for cache invalidation
// ---------------------------------------------------------------------------
export const queryKeys = {
  dashboard: ["dashboard"] as const,
  trades: ["trades"] as const,
  ticker: (ticker: string, range: string) => ["ticker", ticker, range] as const,
  backtest: (runId?: string) => ["backtest", runId ?? "all"] as const,
  backtestRuns: ["backtest-runs"] as const,
};

// ---------------------------------------------------------------------------
// Fetch functions
// ---------------------------------------------------------------------------
async function fetchDashboard(): Promise<DashboardResponse> {
  const res = await apiClient.get<DashboardResponse>("/api/dashboard");
  return res.data;
}

async function fetchTrades(): Promise<Trade[]> {
  const res = await apiClient.get<Trade[]>("/api/trades");
  return res.data;
}

async function fetchTickerChart(ticker: string, range: string): Promise<TickerChartResponse> {
  const res = await apiClient.get<TickerChartResponse>(
    `/api/ticker/${encodeURIComponent(ticker)}?range=${encodeURIComponent(range)}`,
  );
  return res.data;
}

async function fetchBacktest(runId?: string): Promise<BacktestResponse> {
  const params = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  const res = await apiClient.get<BacktestResponse>(`/api/backtest${params}`);
  return res.data;
}

async function fetchBacktestRuns(): Promise<BacktestRunListResponse> {
  const res = await apiClient.get<BacktestRunListResponse>("/api/backtest/runs");
  return res.data;
}

// ---------------------------------------------------------------------------
// Hooks — each component renders its own skeleton while loading,
// so the server and client always produce the same initial HTML.
// ---------------------------------------------------------------------------
export function useDashboard() {
  return useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: fetchDashboard,
    staleTime: 20_000,
    refetchInterval: 20_000,
  });
}

export function useTrades() {
  return useQuery({
    queryKey: queryKeys.trades,
    queryFn: fetchTrades,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  });
}

export function useBacktest(runId?: string) {
  return useQuery({
    queryKey: queryKeys.backtest(runId),
    queryFn: () => fetchBacktest(runId),
    staleTime: 5 * 60_000,
  });
}

export function useBacktestRuns() {
  return useQuery({
    queryKey: queryKeys.backtestRuns,
    queryFn: fetchBacktestRuns,
    staleTime: 5 * 60_000,
  });
}

export function useTickerChart(ticker: string | null, range: string) {
  return useQuery({
    queryKey: queryKeys.ticker(ticker ?? "", range),
    queryFn: () => fetchTickerChart(ticker!, range),
    enabled: !!ticker,
    staleTime: 60_000,
  });
}
