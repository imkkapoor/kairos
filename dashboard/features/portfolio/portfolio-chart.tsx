"use client";

import { useMemo } from "react";
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import { useDashboard } from "@/lib/api/queries";
import { PortfolioChartSkeleton } from "./portfolio-chart-skeleton";

const INITIAL_CAPITAL = 100_000;

const chartConfig = {
  total_value: {
    label: "Portfolio Value",
    color: "var(--chart-1)",
  },
} satisfies ChartConfig;

const PROFIT_COLOR = "var(--profit)";
const LOSS_COLOR   = "var(--loss)";

export function PortfolioChart() {
  const { data: dashboardData, isLoading, isError, error } = useDashboard();
  const data = dashboardData?.portfolio ?? null;

  const liveValue = useMemo(() => {
    if (!data) return 0;
    const positionsValue = Object.values(data.positions).reduce((sum, pos) => {
      if (pos.price == null) return sum;
      return sum + pos.price * pos.qty * (pos.fx_rate ?? 1);
    }, 0);
    return data.cash + positionsValue;
  }, [data]);

  const chartData = useMemo(() => {
    if (!data?.history) return [];

    const todayISO = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD

    // Older days: group by date and average values; today: keep each point at hour precision
    const olderMap = new Map<string, { sum: number; count: number }>();
    const todayItems: { label: string; total_value: number }[] = [];

    for (const point of data.history) {
      const d = new Date(point.time);
      const dateISO = d.toLocaleDateString("en-CA");
      if (dateISO === todayISO) {
        todayItems.push({
          label: d.toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false }),
          total_value: point.total_value,
        });
      } else {
        const entry = olderMap.get(dateISO);
        if (entry) {
          entry.sum += point.total_value;
          entry.count += 1;
        } else {
          olderMap.set(dateISO, { sum: point.total_value, count: 1 });
        }
      }
    }

    const older = Array.from(olderMap.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([dateISO, { sum, count }]) => ({
        date: new Date(dateISO).toLocaleDateString("en-CA", { month: "short", day: "numeric" }),
        total_value: sum / count,
        isLive: false,
      }));

    const liveLabel = new Date().toLocaleTimeString("en-CA", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    const allPoints = [
      ...older,
      ...todayItems.map((pt) => ({ date: pt.label, total_value: pt.total_value, isLive: false })),
    ];
    const last = allPoints.at(-1);
    if (last?.date === liveLabel) {
      allPoints[allPoints.length - 1] = { date: liveLabel, total_value: liveValue, isLive: true };
    } else {
      allPoints.push({ date: liveLabel, total_value: liveValue, isLive: true });
    }

    return allPoints;
  }, [data, liveValue]);

  if (isError) throw error;
  if (isLoading || !data) return <PortfolioChartSkeleton />;

  const pnl = liveValue - INITIAL_CAPITAL;
  const pnlPct = (pnl / INITIAL_CAPITAL) * 100;
  const lineColor = pnl >= 0 ? PROFIT_COLOR : LOSS_COLOR;

  if (!chartData.length)
    return (
      <p className="text-sm text-muted-foreground">No history available.</p>
    );

  return (
    <Card className="bg-surface">
      <CardHeader>
        <CardTitle>Total Value</CardTitle>
        {pnlPct != null && (
          <CardDescription>
            vs. initial capital of {INITIAL_CAPITAL.toLocaleString()}:{" "}
            <span
              className={`font-mono font-medium ${
                pnl >= 0 ? "text-profit" : "text-loss"
              }`}
            >
              {pnl >= 0 ? "+" : ""}
              {pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}{" "}
              ({pnl >= 0 ? "+" : ""}
              {pnlPct.toFixed(2)}%)
            </span>
          </CardDescription>
        )}
      </CardHeader>
      <CardContent>
        <ChartContainer config={chartConfig} className="h-64 w-full ">
          <AreaChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="fill-portfolio" x1="0" y1="0" x2="0" y2="1">
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
              tick={{ fontSize: 11 }}
              interval="preserveStartEnd"
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              tickMargin={8}
              tick={{ fontSize: 11 }}
              tickFormatter={(v: number) =>
                v.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 })
              }
              domain={["auto", "auto"]}
            />
            <ChartTooltip
              content={
                <ChartTooltipContent
                  formatter={(value) =>
                    (value as number).toLocaleString(undefined, { minimumFractionDigits: 2 })
                  }
                />
              }
            />
            <Area
              type="monotone"
              dataKey="total_value"
              stroke={lineColor}
              strokeWidth={2}
              fill="url(#fill-portfolio)"
              dot={false}
              activeDot={{ r: 4 }}
            />
          </AreaChart>
        </ChartContainer>
      </CardContent>
    </Card>
  );
}
