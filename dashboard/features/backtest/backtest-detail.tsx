"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useBacktest } from "@/lib/api/queries";
import { DataTable } from "@/components/ui/data-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { backtestColumns } from "./columns";
import type { BacktestRun, BacktestSummary } from "@/lib/types/api";

function pct(v: number | null, d = 1) {
  return v == null ? "—" : `${(v * 100).toFixed(d)}%`;
}
function num(v: number | null, d = 2) {
  return v == null ? "—" : v.toFixed(d);
}

/** Derive avg_vix / pct_days_elevated from the individual window rows for this config. */
function useVolStats(
  runs: BacktestRun[],
  configName: string,
): { avgVix: number | null; pctElevated: number | null; isFiltered: boolean } {
  const rows = runs.filter((r) => r.config_name === configName);
  const isFiltered = rows.some((r) => r.use_vol_filter);
  if (!isFiltered) return { avgVix: null, pctElevated: null, isFiltered: false };
  const vixVals = rows.map((r) => r.avg_vix).filter((v): v is number => v != null);
  const elevVals = rows.map((r) => r.pct_days_elevated).filter((v): v is number => v != null);
  const avgVix = vixVals.length ? vixVals.reduce((a, b) => a + b, 0) / vixVals.length : null;
  const pctElevated = elevVals.length ? elevVals.reduce((a, b) => a + b, 0) / elevVals.length : null;
  return { avgVix, pctElevated, isFiltered };
}

function SummaryCard({ s, runs }: { s: BacktestSummary; runs: BacktestRun[] }) {
  const { avgVix, pctElevated, isFiltered } = useVolStats(runs, s.config_name);
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 font-mono text-xs">
          {s.config_name}
          {isFiltered && (
            <span className="rounded-sm bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              VIX
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <span className="text-muted-foreground">Avg Sharpe</span>
        <span className={s.avg_sharpe != null && s.avg_sharpe >= 1 ? "text-profit font-medium" : "font-medium"}>
          {num(s.avg_sharpe)}
        </span>
        <span className="text-muted-foreground">Avg CAGR</span>
        <span className="font-medium">{pct(s.avg_cagr)}</span>
        <span className="text-muted-foreground">Avg Max DD</span>
        <span className="text-loss font-medium">{pct(s.avg_max_dd)}</span>
        <span className="text-muted-foreground">Win Rate</span>
        <span className="font-medium">{pct(s.avg_win_rate)}</span>
        <span className="text-muted-foreground">Windows</span>
        <span className="font-medium">{s.windows_tested}</span>
        {isFiltered && avgVix != null && (
          <>
            <span className="text-muted-foreground">Avg VIX</span>
            <span className="font-medium tabular-nums">{avgVix.toFixed(1)}</span>
          </>
        )}
        {isFiltered && pctElevated != null && (
          <>
            <span className="text-muted-foreground">High+Extreme Days</span>
            <span className="font-medium tabular-nums">{pct(pctElevated)}</span>
          </>
        )}
      </CardContent>
    </Card>
  );
}

interface BacktestDetailProps {
  runId: string;
}

export function BacktestDetail({ runId }: BacktestDetailProps) {
  const { data, isLoading, isError, error } = useBacktest(runId);
  const [activeConfig, setActiveConfig] = useState<string | null>(null);

  if (isError) throw error;

  if (isLoading || !data) {
    return (
      <div className="space-y-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <Card key={i} size="sm">
              <CardHeader>
                <div className="h-3 w-24 animate-pulse rounded bg-muted" />
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {Array.from({ length: 5 }).map((_, j) => (
                    <div key={j} className="h-3 w-full animate-pulse rounded bg-muted" />
                  ))}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
        <div className="h-64 animate-pulse rounded-lg bg-muted" />
      </div>
    );
  }

  const configs = Array.from(new Set(data.runs.map((r) => r.config_name)));
  const selected = activeConfig ?? configs[0] ?? null;
  const filteredRuns = selected
    ? data.runs.filter((r) => r.config_name === selected)
    : data.runs;

  return (
    <div className="space-y-6">
      {/* Back link + UUID breadcrumb */}
      <div className="flex items-center gap-2 text-sm">
        <Link
          href="/backtest"
          className="flex items-center gap-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          All Runs
        </Link>
        <span className="text-muted-foreground/40">·</span>
        <span className="font-mono text-xs text-muted-foreground">{runId}</span>
      </div>

      {/* Per-config summary cards */}
      {data.summary.length > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {data.summary.map((s) => (
            <SummaryCard key={s.config_name} s={s} runs={data.runs} />
          ))}
        </div>
      )}

      {/* Config filter + per-window table */}
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {configs.map((c) => (
            <button
              key={c}
              onClick={() => setActiveConfig(c === selected ? null : c)}
              className={[
                "rounded-md border px-3 py-1 text-xs font-mono transition-colors",
                c === selected
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border hover:bg-muted",
              ].join(" ")}
            >
              {c}
            </button>
          ))}
        </div>

        <DataTable columns={backtestColumns} data={filteredRuns} filters={[]} />
      </div>
    </div>
  );
}
