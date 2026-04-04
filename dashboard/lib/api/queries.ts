import { useQuery } from "@tanstack/react-query";

import apiClient from "./client";
import type {
  DashboardResponse,
  Trade,
} from "../types/api";

// ---------------------------------------------------------------------------
// Query keys — single source of truth for cache invalidation
// ---------------------------------------------------------------------------
export const queryKeys = {
  dashboard: ["dashboard"] as const,
  trades: ["trades"] as const,
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
