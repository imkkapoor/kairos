"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft, ChevronDown, ChevronRight } from "lucide-react";
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
function curr(v: number | null) {
  if (v == null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

function avgOf(rows: BacktestRun[], key: keyof BacktestRun): number | null {
  const vals = rows
    .map((r) => r[key] as number | null)
    .filter((v): v is number => v != null);
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

function useConfigStats(runs: BacktestRun[], configName: string) {
  const rows = runs.filter((r) => r.config_name === configName);
  const isFiltered = rows.some((r) => r.use_vol_filter);
  const hasSoftCb = rows.some((r) => r.use_soft_cb);
  const hasCb = !hasSoftCb && rows.some((r) => r.use_circuit_breaker);
  const hasCrisisLimits = rows.some((r) => r.use_crisis_pos_limits);
  return {
    isFiltered,
    hasSoftCb,
    hasCb,
    hasCrisisLimits,
    avgVix: avgOf(rows, "avg_vix"),
    pctElevated: avgOf(rows, "pct_days_elevated"),
    pctDaysSpike: avgOf(rows, "pct_days_spike"),
    avgCbMult: avgOf(rows, "avg_cb_mult"),
    pctDaysChatterHeld: avgOf(rows, "pct_days_chatter_held"),
    pctDaysBreakerActive: avgOf(rows, "pct_days_breaker_active"),
    avgProfitFactor: avgOf(rows, "profit_factor"),
    avgAnnVol: avgOf(rows, "annualized_vol"),
    avgWinPct: avgOf(rows, "avg_win_pct"),
    avgLossPct: avgOf(rows, "avg_loss_pct"),
  };
}

function SummaryCard({ s, runs }: { s: BacktestSummary; runs: BacktestRun[] }) {
  const stats = useConfigStats(runs, s.config_name);
  const { isFiltered, hasSoftCb, hasCb, hasCrisisLimits } = stats;
  const hasRiskOverlays = isFiltered || hasSoftCb || hasCb || hasCrisisLimits;

  const configRuns = runs.filter((r) => r.config_name === s.config_name);
  const capitalMode = configRuns[0]?.capital_mode ?? "capital_refresh";
  const isCompounded = capitalMode === "capital_compounded";

  // For compounded: single run, use its values directly
  const singleRun = isCompounded ? configRuns[0] ?? null : null;
  const finalPortfolioValue = singleRun?.final_value_usd ?? null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-center gap-1 font-mono text-xs">
          {s.config_name}
          {isFiltered && (
            <span className="rounded-sm bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              VIX
            </span>
          )}
          {hasSoftCb && (
            <span className="rounded-sm bg-orange-100 px-1 py-0.5 text-[10px] font-medium text-orange-800 dark:bg-orange-900/40 dark:text-orange-300">
              SCB
            </span>
          )}
          {hasCb && (
            <span className="rounded-sm bg-red-100 px-1 py-0.5 text-[10px] font-medium text-red-800 dark:bg-red-900/40 dark:text-red-300">
              CB
            </span>
          )}
          {hasCrisisLimits && (
            <span className="rounded-sm bg-purple-100 px-1 py-0.5 text-[10px] font-medium text-purple-800 dark:bg-purple-900/40 dark:text-purple-300">
              CPL
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-xs">
        {/* ── Key metrics ── */}
        <div className="flex flex-wrap gap-6">
          {isCompounded && (
            <div>
              <p className="text-[10px] text-muted-foreground">Final Value</p>
              <p className="text-lg font-semibold tabular-nums">
                {curr(finalPortfolioValue)}
              </p>
            </div>
          )}
          {isCompounded && (
            <div>
              <p className="text-[10px] text-muted-foreground">Total PnL</p>
              <p
                className={`text-lg font-semibold tabular-nums ${
                  s.total_pnl != null && s.total_pnl >= 0
                    ? "text-profit"
                    : "text-loss"
                }`}
              >
                {curr(s.total_pnl)}
              </p>
            </div>
          )}
          <div>
            <p className="text-[10px] text-muted-foreground">{isCompounded ? "Sharpe" : "Avg Sharpe"}</p>
            <p
              className={`text-lg font-semibold tabular-nums ${
                s.avg_sharpe != null && s.avg_sharpe >= 1 ? "text-profit" : ""
              }`}
            >
              {num(s.avg_sharpe)}
            </p>
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">{isCompounded ? "CAGR" : "Avg CAGR"}</p>
            <p className="text-lg font-semibold tabular-nums">
              {pct(s.avg_cagr)}
            </p>
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">{isCompounded ? "Max DD" : "Avg Max DD"}</p>
            <p className="text-lg font-semibold tabular-nums text-loss">
              {pct(s.avg_max_dd)}
            </p>
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">Win Rate</p>
            <p className="text-lg font-semibold tabular-nums">
              {pct(s.avg_win_rate)}
            </p>
          </div>
        </div>

        <div className="border-t border-border" />

        {/* ── Detail stats ── */}
        <div className="grid grid-cols-2 gap-x-8 gap-y-1.5 sm:grid-cols-3">
          <div className="flex items-baseline justify-between gap-2">
            <span className="whitespace-nowrap text-muted-foreground">{isCompounded ? "Calmar" : "Avg Calmar"}</span>
            <span className="font-medium tabular-nums">{num(s.avg_calmar)}</span>
          </div>
          {isCompounded && singleRun && (
            <div className="flex items-baseline justify-between gap-2">
              <span className="whitespace-nowrap text-muted-foreground">Period</span>
              <span className="font-mono font-medium tabular-nums text-xs">
                {(singleRun.window_start as string).slice(0, 7)} → {(singleRun.window_end as string).slice(0, 7)}
              </span>
            </div>
          )}
          {isCompounded && singleRun && (
            <div className="flex items-baseline justify-between gap-2">
              <span className="whitespace-nowrap text-muted-foreground">Trades</span>
              <span className="font-medium tabular-nums">{singleRun.total_trades}</span>
            </div>
          )}
          {!isCompounded && (
            <div className="flex items-baseline justify-between gap-2">
              <span className="whitespace-nowrap text-muted-foreground">Avg PnL / Window</span>
              <span
                className={`font-medium tabular-nums ${
                  s.avg_pnl_pct != null && s.avg_pnl_pct >= 0
                    ? "text-profit"
                    : "text-loss"
                }`}
              >
                {pct(s.avg_pnl_pct)}
              </span>
            </div>
          )}
          {!isCompounded && (
            <div className="flex items-baseline justify-between gap-2">
              <span className="whitespace-nowrap text-muted-foreground">Windows</span>
              <span className="font-medium tabular-nums">{s.windows_tested}</span>
            </div>
          )}
          <div className="flex items-baseline justify-between gap-2">
            <span className="whitespace-nowrap text-muted-foreground">Profit Factor</span>
            <span className="font-medium tabular-nums">{num(stats.avgProfitFactor)}</span>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <span className="whitespace-nowrap text-muted-foreground">Avg Win %</span>
            <span className="font-medium tabular-nums text-profit">{pct(stats.avgWinPct)}</span>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <span className="whitespace-nowrap text-muted-foreground">Avg Loss %</span>
            <span className="font-medium tabular-nums text-loss">{pct(stats.avgLossPct)}</span>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <span className="whitespace-nowrap text-muted-foreground">Ann. Volatility</span>
            <span className="font-medium tabular-nums">{pct(stats.avgAnnVol)}</span>
          </div>
        </div>

        {/* ── Risk overlay stats ── */}
        {hasRiskOverlays && (
          <>
            <div className="border-t border-border" />
            <div className="grid grid-cols-2 gap-x-8 gap-y-1.5 sm:grid-cols-3">
              {isFiltered && stats.avgVix != null && (
                <div className="flex items-baseline justify-between gap-2">
                  <span className="whitespace-nowrap text-muted-foreground">Avg VIX</span>
                  <span className="font-medium tabular-nums">{stats.avgVix.toFixed(1)}</span>
                </div>
              )}
              {isFiltered && stats.pctElevated != null && (
                <div className="flex items-baseline justify-between gap-2">
                  <span className="whitespace-nowrap text-muted-foreground">High+Extreme Days</span>
                  <span className="font-medium tabular-nums">{pct(stats.pctElevated)}</span>
                </div>
              )}
              {isFiltered && stats.pctDaysSpike != null && (
                <div className="flex items-baseline justify-between gap-2">
                  <span className="whitespace-nowrap text-muted-foreground">VROC Spike Days</span>
                  <span className="font-medium tabular-nums">{pct(stats.pctDaysSpike)}</span>
                </div>
              )}
              {(hasSoftCb || hasCb) && stats.pctDaysBreakerActive != null && (
                <div className="flex items-baseline justify-between gap-2">
                  <span className="whitespace-nowrap text-muted-foreground">CB Active Days</span>
                  <span className="font-medium tabular-nums">{pct(stats.pctDaysBreakerActive)}</span>
                </div>
              )}
              {hasSoftCb && stats.avgCbMult != null && (
                <div className="flex items-baseline justify-between gap-2">
                  <span className="whitespace-nowrap text-muted-foreground">Avg CB Mult</span>
                  <span className="font-medium tabular-nums">{stats.avgCbMult.toFixed(2)}\u00d7</span>
                </div>
              )}
              {hasSoftCb && stats.pctDaysChatterHeld != null && (
                <div className="flex items-baseline justify-between gap-2">
                  <span className="whitespace-nowrap text-muted-foreground">Chatter Hold Days</span>
                  <span className="font-medium tabular-nums">{pct(stats.pctDaysChatterHeld)}</span>
                </div>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ConfigPanel({ config }: { config: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border bg-surface text-xs">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" />
        )}
        <span className="font-medium">Config Parameters</span>
      </button>
      {open && (
        <div className="border-t px-3 pb-3 pt-2">
          <pre className="overflow-auto whitespace-pre font-mono text-[11px] text-muted-foreground">
            {JSON.stringify(config, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

interface BacktestDetailProps {
  runId: string;
}

export function BacktestDetail({ runId }: BacktestDetailProps) {
  const { data, isLoading, isError, error } = useBacktest(runId);

  if (isError) throw error;

  if (isLoading || !data) {
    return (
      <div className="space-y-6">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 1 }).map((_, i) => (
            <Card key={i}>
              <CardHeader>
                <div className="h-3 w-40 animate-pulse rounded bg-muted" />
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {Array.from({ length: 8 }).map((_, j) => (
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

  const firstRun = data.runs[0];

  return (
    <div className="space-y-6">
      {/* Back link + UUID + category breadcrumb */}
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
        {firstRun?.category && (
          <>
            <span className="text-muted-foreground/40">·</span>
            <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase text-muted-foreground">
              {firstRun.category}
            </span>
          </>
        )}
        {firstRun?.capital_mode && (
          <>
            <span className="text-muted-foreground/40">·</span>
            <span
              className={`rounded-sm px-1.5 py-0.5 text-[10px] font-medium ${
                firstRun.capital_mode === "capital_compounded"
                  ? "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300"
                  : "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300"
              }`}
            >
              {firstRun.capital_mode === "capital_compounded" ? "Compounded" : "Refresh"}
            </span>
          </>
        )}
        {firstRun?.config_origin && (
          <>
            <span className="text-muted-foreground/40">·</span>
            <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px] font-medium capitalize text-muted-foreground">
              {firstRun.config_origin}
            </span>
          </>
        )}
      </div>

      {/* Per-config summary cards */}
      {data.summary.length > 0 && (
        <div className="space-y-3">
          {data.summary.map((s) => (
            <SummaryCard key={s.config_name} s={s} runs={data.runs} />
          ))}
        </div>
      )}

      {/* Config parameters (collapsible) */}
      {firstRun?.config && (
        <ConfigPanel config={firstRun.config as Record<string, unknown>} />
      )}

      {/* Per-window table — only shown in refresh mode */}
      {firstRun?.capital_mode !== "capital_compounded" && (
        <DataTable columns={backtestColumns} data={data.runs} filters={[]} />
      )}
    </div>
  );
}

