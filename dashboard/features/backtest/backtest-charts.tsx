"use client";

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import type { BacktestAnalytics, BacktestRun } from "@/lib/types/api";

// ---------------------------------------------------------------------------
// Equity Curve
// ---------------------------------------------------------------------------

const equityConfig = {
  value: { label: "Portfolio Value", color: "var(--chart-1)" },
} satisfies ChartConfig;

// 20 distinct colours for per-window lines
const WINDOW_COLORS = [
  "#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
  "#937860", "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD",
  "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
  "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
];

interface EquityCurveChartProps {
  analytics: BacktestAnalytics;
  /** Per-window analytics for refresh mode (one line per window) */
  perWindowAnalytics?: BacktestAnalytics[];
  /** Backtest runs — used to label windows by index */
  runs?: BacktestRun[];
}

export function EquityCurveChart({ analytics, perWindowAnalytics, runs }: EquityCurveChartProps) {
  // Multi-window refresh mode
  const multiData = useMemo(() => {
    if (!perWindowAnalytics?.length) return null;

    const runMap = new Map((runs ?? []).map((r) => [r.id, r]));
    const windows = perWindowAnalytics
      .map((a) => {
        const run = runMap.get(a.backtest_id);
        return {
          windowIndex: run?.window_index ?? 0,
          start: run?.window_start?.slice(0, 7) ?? "",
          end: run?.window_end?.slice(0, 7) ?? "",
          curve: a.equity_curve ?? [],
        };
      })
      .filter((w) => w.curve.length >= 2)
      .sort((a, b) => a.windowIndex - b.windowIndex);

    if (!windows.length) return null;

    const maxLen = Math.max(...windows.map((w) => w.curve.length));
    const rows: Record<string, number | undefined>[] = [];

    for (let d = 0; d < maxLen; d++) {
      const row: Record<string, number | undefined> = { day: d };
      for (const w of windows) {
        if (d < w.curve.length) {
          row[`w${w.windowIndex}`] = w.curve[d];
        }
      }
      rows.push(row);
    }

    return { rows, windows };
  }, [perWindowAnalytics, runs]);

  // Single equity curve (compounded mode or single-window)
  const singleData = useMemo(() => {
    if (!analytics.equity_curve?.length) return [];
    return analytics.timestamps.map((ts, i) => ({
      date: ts,
      value: analytics.equity_curve[i],
    }));
  }, [analytics]);

  // Multi-window line chart
  if (multiData) {
    const { rows, windows } = multiData;
    const windowLabel = (w: (typeof windows)[number]) =>
      w.start && w.end ? `W${w.windowIndex}: ${w.start} → ${w.end}` : `W${w.windowIndex}`;

    const windowConfig = Object.fromEntries(
      windows.map((w, i) => [
        `w${w.windowIndex}`,
        { label: windowLabel(w), color: WINDOW_COLORS[i % WINDOW_COLORS.length] },
      ]),
    ) satisfies ChartConfig;

    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Equity Curves by Window</CardTitle>
        </CardHeader>
        <CardContent>
          <ChartContainer config={windowConfig} className="h-[40rem] w-full">
            <LineChart data={rows} margin={{ top: 4, right: 12, bottom: 12, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} className="stroke-border" />
              <XAxis
                dataKey="day"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                tick={{ fontSize: 10 }}
                label={{ value: "Trading Days", position: "insideBottomRight", offset: -4, fontSize: 10, fill: "var(--muted-foreground)" }}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                tick={{ fontSize: 10 }}
                tickFormatter={(v: number) =>
                  v.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 })
                }
                domain={["auto", "auto"]}
              />
              <ChartTooltip
                content={({ active, payload, label }) => {
                  if (!active || !payload?.length) return null;
                  return (
                    <div className="min-w-36 rounded-lg border border-border/50 bg-background px-2.5 py-1.5 text-xs shadow-xl">
                      <p className="mb-1 font-medium">Day {label}</p>
                      <div className="grid gap-1">
                        {payload
                          .filter((p) => p.value != null)
                          .map((p) => (
                            <div key={String(p.dataKey)} className="flex items-center gap-2">
                              <span
                                className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]"
                                style={{ backgroundColor: p.color }}
                              />
                              <span className="text-muted-foreground">{p.name}</span>
                              <span className="ml-auto font-mono font-medium tabular-nums">
                                ${(p.value as number).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                              </span>
                            </div>
                          ))}
                      </div>
                    </div>
                  );
                }}
              />
              {windows.map((w, i) => (
                <Line
                  key={w.windowIndex}
                  type="monotone"
                  dataKey={`w${w.windowIndex}`}
                  name={windowLabel(w)}
                  stroke={WINDOW_COLORS[i % WINDOW_COLORS.length]}
                  strokeWidth={1.5}
                  dot={false}
                  activeDot={{ r: 3 }}
                  connectNulls={false}
                />
              ))}
            </LineChart>
          </ChartContainer>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 px-1">
            {windows.map((w, i) => (
              <span key={w.windowIndex} className="flex items-center gap-1 text-[10px] text-muted-foreground">
                <span
                  className="inline-block h-2 w-3 shrink-0 rounded-sm"
                  style={{ backgroundColor: WINDOW_COLORS[i % WINDOW_COLORS.length] }}
                />
                {windowLabel(w)}
              </span>
            ))}
          </div>
        </CardContent>
      </Card>
    );
  }

  // Single equity curve (compounded mode)
  if (!singleData.length) return null;

  const firstVal = singleData[0].value;
  const lastVal = singleData[singleData.length - 1].value;
  const lineColor = lastVal >= firstVal ? "var(--profit)" : "var(--loss)";

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Equity Curve</CardTitle>
      </CardHeader>
      <CardContent>
        <ChartContainer config={equityConfig} className="h-[32rem] w-full">
          <AreaChart data={singleData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="fill-equity" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={lineColor} stopOpacity={0.3} />
                <stop offset="95%" stopColor={lineColor} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" vertical={false} className="stroke-border" />
            <XAxis
              dataKey="date"
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              tick={{ fontSize: 10 }}
              interval="preserveStartEnd"
              tickFormatter={(v: string) => v.slice(0, 7)}
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              tick={{ fontSize: 10 }}
              tickFormatter={(v: number) =>
                v.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 })
              }
              domain={["auto", "auto"]}
            />
            <ChartTooltip
              content={
                <ChartTooltipContent
                  labelFormatter={(label) => String(label)}
                  formatter={(value) =>
                    `$${(value as number).toLocaleString(undefined, { minimumFractionDigits: 2 })}`
                  }
                />
              }
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke={lineColor}
              strokeWidth={2}
              fill="url(#fill-equity)"
              dot={false}
              activeDot={{ r: 3 }}
            />
          </AreaChart>
        </ChartContainer>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Drawdown Curve
// ---------------------------------------------------------------------------

const drawdownConfig = {
  drawdown: { label: "Drawdown", color: "var(--loss)" },
} satisfies ChartConfig;

export function DrawdownCurveChart({ analytics }: { analytics: BacktestAnalytics }) {
  const data = useMemo(() => {
    if (!analytics.drawdown_curve?.length) return [];
    return analytics.timestamps.map((ts, i) => ({
      date: ts,
      drawdown: analytics.drawdown_curve[i] * 100,
    }));
  }, [analytics]);

  if (!data.length) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Drawdown</CardTitle>
      </CardHeader>
      <CardContent>
        <ChartContainer config={drawdownConfig} className="h-[20rem] w-full">
          <AreaChart data={data} margin={{ top: 4, right: 12, bottom: 12, left: 0 }}>
            <defs>
              <linearGradient id="fill-dd" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="var(--loss)" stopOpacity={0.3} />
                <stop offset="95%" stopColor="var(--loss)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" vertical={false} className="stroke-border" />
            <XAxis
              dataKey="date"
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              tick={{ fontSize: 10 }}
              interval="preserveStartEnd"
              tickFormatter={(v: string) => v.slice(0, 7)}
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              tick={{ fontSize: 10 }}
              tickFormatter={(v: number) => `${v.toFixed(1)}%`}
              domain={["auto", 15]}
            />
            <ChartTooltip
              content={
                <ChartTooltipContent
                  labelFormatter={(label) => String(label)}
                  formatter={(value) => `${(value as number).toFixed(2)}%`}
                />
              }
            />
            <Area
              type="monotone"
              dataKey="drawdown"
              stroke="var(--loss)"
              strokeWidth={1.5}
              fill="transparent"
              dot={false}
              activeDot={{ r: 3 }}
            />
          </AreaChart>
        </ChartContainer>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Monthly Returns Heatmap
// ---------------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function heatColor(v: number): string {
  if (v > 0.08) return "bg-emerald-600 text-white";
  if (v > 0.04) return "bg-emerald-500 text-white";
  if (v > 0.02) return "bg-emerald-400 text-white";
  if (v > 0) return "bg-emerald-300 text-emerald-900";
  if (v === 0) return "bg-muted text-muted-foreground";
  if (v > -0.02) return "bg-red-300 text-red-900";
  if (v > -0.04) return "bg-red-400 text-white";
  if (v > -0.08) return "bg-red-500 text-white";
  return "bg-red-600 text-white";
}

export function MonthlyReturnsHeatmap({ analytics }: { analytics: BacktestAnalytics }) {
  const { monthly_returns } = analytics;
  if (!monthly_returns || !Object.keys(monthly_returns).length) return null;

  const years = Object.keys(monthly_returns).sort();

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Monthly Returns</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="text-[10px] tabular-nums border-separate border-spacing-0.5">
          <thead>
            <tr>
              <th className="pr-3 py-1 text-left font-medium text-muted-foreground whitespace-nowrap">Year</th>
              {MONTHS.map((m) => (
                <th key={m} className="w-10 text-center font-medium text-muted-foreground">
                  {m}
                </th>
              ))}
              <th className="w-12 pl-3 text-center font-medium text-muted-foreground">Annual</th>
            </tr>
          </thead>
          <tbody>
            {years.map((yr) => {
              const row = monthly_returns[yr];
              return (
                <tr key={yr}>
                  <td className="pr-3 py-0.5 font-mono font-medium whitespace-nowrap">{yr}</td>
                  {MONTHS.map((m) => {
                    const v = row?.[m];
                    return (
                      <td key={m} className="p-0 text-center">
                        {v != null ? (
                          <span
                            className={`flex items-center justify-center w-10 h-10 rounded font-mono ${heatColor(v)}`}
                          >
                            {(v * 100).toFixed(1)}%
                          </span>
                        ) : (
                          <span className="flex items-center justify-center w-10 h-10 text-muted-foreground/40">—</span>
                        )}
                      </td>
                    );
                  })}
                  <td className="pl-3 text-center">
                    {row?.annual != null ? (
                      <span
                        className={`flex items-center justify-center w-12 h-10 rounded font-mono font-semibold ${heatColor(row.annual)}`}
                      >
                        {(row.annual * 100).toFixed(1)}%
                      </span>
                    ) : (
                      <span className="flex items-center justify-center w-12 h-10 text-muted-foreground/40">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Regime Stats Heatmap
// ---------------------------------------------------------------------------

function regimeHeatColor(v: number): string {
  if (v > 5000) return "bg-emerald-600 text-white";
  if (v > 2000) return "bg-emerald-500 text-white";
  if (v > 500) return "bg-emerald-400 text-white";
  if (v > 0) return "bg-emerald-300 text-emerald-900";
  if (v === 0) return "bg-muted text-muted-foreground";
  if (v > -500) return "bg-red-300 text-red-900";
  if (v > -2000) return "bg-red-400 text-white";
  if (v > -5000) return "bg-red-500 text-white";
  return "bg-red-600 text-white";
}

export function RegimeStatsHeatmap({ analytics }: { analytics: BacktestAnalytics }) {
  const { regime_stats } = analytics;
  if (!regime_stats || !Object.keys(regime_stats).length) return null;

  const strategies = Object.keys(regime_stats).sort();
  const regimeSet = new Set<string>();
  for (const strat of strategies) {
    for (const r of Object.keys(regime_stats[strat])) {
      regimeSet.add(r);
    }
  }
  const regimes = Array.from(regimeSet).sort();

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Strategy × Regime Performance</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="text-[10px] tabular-nums border-separate border-spacing-0.5">
          <thead>
            <tr>
              <th className="pr-4 py-1 text-left font-medium text-muted-foreground whitespace-nowrap">Strategy</th>
              {regimes.map((r) => (
                <th key={r} className="w-20 text-center font-medium capitalize text-muted-foreground">
                  {r}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {strategies.map((strat) => (
              <tr key={strat}>
                <td className="pr-4 py-0.5 font-mono font-medium whitespace-nowrap">{strat}</td>
                {regimes.map((r) => {
                  const v = regime_stats[strat]?.[r];
                  return (
                    <td key={r} className="p-0 text-center">
                      {v != null ? (
                        <span
                          className={`flex items-center justify-center w-20 h-10 rounded font-mono ${regimeHeatColor(v)}`}
                        >
                          ${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                        </span>
                      ) : (
                        <span className="flex items-center justify-center w-20 h-10 text-muted-foreground/40">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
