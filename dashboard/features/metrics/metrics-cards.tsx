"use client";

import { Area, AreaChart, ResponsiveContainer, YAxis } from "recharts";
import { TrendingUp, TrendingDown } from "lucide-react";

import { useDashboard } from "@/lib/api/queries";
import { MetricsCardsSkeleton } from "./metrics-cards-skeleton";
import type { MetricData } from "@/lib/types/api";

// ---------------------------------------------------------------------------
// Config: map API ticker → display label + price format
// ---------------------------------------------------------------------------

const METRIC_CONFIG: Record<
  string,
  { label: string; prefix?: string; decimals: number }
> = {
  "^GSPC": { label: "S&P 500", decimals: 2 },
  "CL=F": { label: "Crude Oil", prefix: "$", decimals: 2 },
  "CAD=X": { label: "CAD / USD", decimals: 4 },
  "^VIX": { label: "VIX", decimals: 2 },
};

const TICKER_ORDER = ["^GSPC", "CL=F", "CAD=X", "^VIX"];

// ---------------------------------------------------------------------------
// Single metric card
// ---------------------------------------------------------------------------

function MetricCard({
  ticker,
  data,
}: {
  ticker: string;
  data: MetricData | null;
}) {
  const cfg = METRIC_CONFIG[ticker] ?? { label: ticker, decimals: 2 };
  const positive = data ? (data.change_pct ?? 0) >= 0 : true;
  const color = positive ? "#22c55e" : "#ef4444";
  const bgGradientId = `grad-${ticker.replace(/[^a-zA-Z]/g, "")}`;

  const fmt = (v: number) =>
    `${cfg.prefix ?? ""}${v.toLocaleString(undefined, {
      minimumFractionDigits: cfg.decimals,
      maximumFractionDigits: cfg.decimals,
    })}`;

  const sparkBars = data?.bars?.map((b) => ({ v: b.close })) ?? [];

  // Compute tight Y domain so intraday variation is visible
  const yDomain = (() => {
    if (!sparkBars.length) return ["auto", "auto"] as const;
    const vals = sparkBars.map((b) => b.v);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const pad = (max - min) * 0.15 || max * 0.001;
    return [min - pad, max + pad] as const;
  })();

  return (
    <div className="relative rounded-xl overflow-hidden bg-surface border box-shadow flex flex-col p-3 gap-2 min-h-[110px]">
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-xs font-medium text-white/60 leading-none">
            {cfg.label}
          </p>
          {data ? (
            <p className="mt-1 text-base font-semibold font-mono text-white leading-none">
              {fmt(data.current)}
            </p>
          ) : (
            <p className="mt-1 text-xs text-white/30">—</p>
          )}
        </div>

        {data?.change_pct != null && (
          <div
            className="flex items-center gap-0.5 text-xs font-semibold shrink-0"
            style={{ color }}
          >
            {positive ? (
              <TrendingUp className="h-3 w-3" strokeWidth={2.5} />
            ) : (
              <TrendingDown className="h-3 w-3" strokeWidth={2.5} />
            )}
            {positive ? "+" : ""}
            {data.change_pct.toFixed(2)}%
          </div>
        )}
      </div>

      {/* Absolute change */}
      {data?.change != null && (
        <p className="text-[11px] font-mono leading-none" style={{ color }}>
          {data.change >= 0 ? "+" : ""}
          {fmt(data.change)}
        </p>
      )}

      {/* Sparkline — tight domain so variation is visible */}
      {sparkBars.length > 1 && (
        <div className="h-11 w-full -mx-0.5 mt-auto">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 200, height: 44 }}>
            <AreaChart
              data={sparkBars}
              margin={{ top: 1, right: 0, left: 0, bottom: 0 }}
            >
              <defs>
                <linearGradient id={bgGradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={color} stopOpacity={0.4} />
                  <stop offset="95%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <YAxis domain={yDomain} hide />
              <Area
                type="monotone"
                dataKey="v"
                stroke={color}
                strokeWidth={1.5}
                fill={`url(#${bgGradientId})`}
                dot={false}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row of 4 cards
// ---------------------------------------------------------------------------

export function MetricsCards() {
  const { data, isLoading, isError, error } = useDashboard();

  if (isError) throw error;
  if (isLoading || !data) return <MetricsCardsSkeleton />;

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {TICKER_ORDER.map((ticker) => (
        <MetricCard
          key={ticker}
          ticker={ticker}
          data={data.metrics?.[ticker] ?? null}
        />
      ))}
    </div>
  );
}
