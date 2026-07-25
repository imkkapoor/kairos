"use client";

import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ColumnDef } from "@tanstack/react-table";

import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataTable } from "@/components/ui/data-table";
import { Skeleton } from "@/components/ui/skeleton";
import { useTickerChart } from "@/lib/api/queries";
import type { Trade } from "@/lib/types/api";
import type { PositionRow } from "./columns";

// ---------------------------------------------------------------------------
// Range config
// ---------------------------------------------------------------------------

const RANGES = ["1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "5Y"] as const;
type Range = (typeof RANGES)[number];

// ---------------------------------------------------------------------------
// Trades table columns (no ticker column — we already know it)
// ---------------------------------------------------------------------------

const drawerTradesColumns: ColumnDef<Trade>[] = [
  {
    accessorKey: "time",
    header: "Time",
    enableSorting: true,
    cell: ({ row }) =>
      new Date(row.getValue("time")).toLocaleString("en-US", {
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      }),
  },
  {
    accessorKey: "side",
    header: "Action",
    enableSorting: false,
    cell: ({ row }) => {
      const side: string = row.getValue("side");
      return (
        <span className={side === "buy" ? "font-semibold text-profit" : "font-semibold text-loss"}>
          {side.toUpperCase()}
        </span>
      );
    },
  },
  { accessorKey: "quantity", header: "Qty", enableSorting: true },
  {
    accessorKey: "fill_price",
    header: "Fill Price",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("fill_price") as number | null;
      return v != null ? <span className="font-mono">${v.toFixed(2)}</span> : "—";
    },
  },
  {
    accessorKey: "stop_loss",
    header: "Stop Loss",
    enableSorting: false,
    cell: ({ row }) => {
      const v = row.getValue("stop_loss") as number | null;
      return v != null ? <span className="font-mono">${v.toFixed(2)}</span> : "—";
    },
  },
  {
    accessorKey: "take_profit",
    header: "Take Profit",
    enableSorting: false,
    cell: ({ row }) => {
      const v = row.getValue("take_profit") as number | null;
      return v != null ? <span className="font-mono">${v.toFixed(2)}</span> : "—";
    },
  },
  {
    accessorKey: "strategy",
    header: "Strategy",
    enableSorting: false,
    cell: ({ row }) => {
      const s: string = row.getValue("strategy") ?? "";
      return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    },
  },
  {
    accessorKey: "reason",
    header: "Reason",
    enableSorting: false,
    cell: ({ row }) => {
      const r: string = row.getValue("reason") ?? "";
      return r.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    },
  },
];

// ---------------------------------------------------------------------------
// X-axis tick formatter
// ---------------------------------------------------------------------------

function makeTickFormatter(range: Range) {
  return (value: number) => {
    const d = new Date(value);
    if (range === "1D") {
      return d.toLocaleTimeString("en-US", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: "America/New_York",
      });
    }
    if (range === "1Y" || range === "5Y") {
      return d.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
    }
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  };
}

// ---------------------------------------------------------------------------
// Custom tooltip
// ---------------------------------------------------------------------------

function ChartTooltipContent({ active, payload }: { active?: boolean; payload?: Array<{ value: number; payload: { time: number } }> }) {
  if (!active || !payload?.length) return null;
  const { time } = payload[0].payload;
  const close = payload[0].value;
  const d = new Date(time);
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="text-muted-foreground">
        {d.toLocaleString("en-US", {
          month: "short",
          day: "numeric",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          hour12: true,
          timeZone: "America/New_York",
        })}
      </p>
      <p className="font-mono font-semibold">${close?.toFixed(2)}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main drawer
// ---------------------------------------------------------------------------

interface TickerDrawerProps {
  position: PositionRow | null;
  onClose: () => void;
}

export function TickerDrawer({ position, onClose }: TickerDrawerProps) {
  const [range, setRange] = useState<Range>("3M");
  const ticker = position?.ticker ?? null;

  const { data, isLoading } = useTickerChart(ticker, range);

  const chartData = useMemo(
    () =>
      (data?.bars ?? [])
        .filter((b) => b.close != null)
        .map((b) => ({ time: new Date(b.time).getTime(), close: b.close! })),
    [data?.bars],
  );

  const buyPoints = useMemo(
    () =>
      (data?.trades ?? [])
        .filter((t) => t.side === "buy")
        .map((t) => ({ x: new Date(t.time).getTime(), y: t.fill_price })),
    [data?.trades],
  );

  const sellPoints = useMemo(
    () =>
      (data?.trades ?? [])
        .filter((t) => t.side === "sell")
        .map((t) => ({ x: new Date(t.time).getTime(), y: t.fill_price })),
    [data?.trades],
  );

  // Y-axis domain that accommodates both bars and trade fill prices
  const yDomain = useMemo<[number, number] | ["auto", "auto"]>(() => {
    const prices = [
      ...chartData.map((d) => d.close),
      ...(data?.trades ?? []).map((t) => t.fill_price),
    ].filter((v) => v != null && isFinite(v)) as number[];
    if (!prices.length) return ["auto", "auto"];
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const pad = (max - min) * 0.06 || max * 0.02;
    return [min - pad, max + pad];
  }, [chartData, data?.trades]);

  const tickFormatter = makeTickFormatter(range);

  // Position summary line
  const pnl =
    position?.price != null && position?.avg_cost != null
      ? (position.price - position.avg_cost) * position.qty * (position.fx_rate ?? 1)
      : null;
  const pnlPct =
    position?.price != null && position?.avg_cost != null
      ? ((position.price - position.avg_cost) / position.avg_cost) * 100
      : null;

  return (
    <Sheet open={!!ticker} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 overflow-y-auto sm:max-w-2xl"
      >
        <SheetHeader className="px-6 pt-6 pb-4">
          <SheetTitle className="flex items-baseline gap-3 text-xl">
            {ticker}
            {position?.price != null && (
              <span className="font-mono text-base font-normal text-foreground">
                ${position.price.toFixed(2)}
              </span>
            )}
            {pnl != null && pnlPct != null && (
              <span
                className={`font-mono text-sm ${pnl >= 0 ? "text-profit" : "text-loss"}`}
              >
                {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)} ({pnl >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%)
              </span>
            )}
          </SheetTitle>
          {position && (
            <p className="text-xs text-muted-foreground">
              {position.qty} shares · avg cost ${position.avg_cost.toFixed(2)} ·{" "}
              {position.strategy.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())} ·{" "}
              {position.currency}
            </p>
          )}
        </SheetHeader>

        {/* Range selector */}
        <div className="flex gap-1 px-6 pb-4">
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                r === range
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {r}
            </button>
          ))}
        </div>

        {/* Chart */}
        <div className="px-4">
          {isLoading ? (
            <Skeleton className="h-56 w-full rounded-md" />
          ) : chartData.length === 0 ? (
            <div className="flex h-56 items-center justify-center text-sm text-muted-foreground">
              No price data available
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={224}>
              <AreaChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="tickerGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="var(--chart-1)" stopOpacity={0.25} />
                    <stop offset="95%" stopColor="var(--chart-1)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="var(--border)" strokeOpacity={0.5} />
                <XAxis
                  dataKey="time"
                  type="number"
                  scale="time"
                  domain={["auto", "auto"]}
                  tickFormatter={tickFormatter}
                  tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={40}
                />
                <YAxis
                  domain={yDomain}
                  tickFormatter={(v: number) => `$${v.toFixed(2)}`}
                  tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
                  tickLine={false}
                  axisLine={false}
                  width={60}
                />
                <Tooltip content={<ChartTooltipContent />} />
                <Area
                  type="monotone"
                  dataKey="close"
                  stroke="var(--chart-1)"
                  strokeWidth={1.5}
                  fill="url(#tickerGradient)"
                  dot={false}
                  isAnimationActive={false}
                />
                {buyPoints.map((pt, i) => (
                  <ReferenceDot
                    key={`buy-${i}`}
                    x={pt.x}
                    y={pt.y}
                    r={5}
                    fill="var(--profit)"
                    stroke="var(--background)"
                    strokeWidth={1.5}
                    label={false as unknown as undefined}
                  />
                ))}
                {sellPoints.map((pt, i) => (
                  <ReferenceDot
                    key={`sell-${i}`}
                    x={pt.x}
                    y={pt.y}
                    r={5}
                    fill="var(--loss)"
                    stroke="var(--background)"
                    strokeWidth={1.5}
                    label={false as unknown as undefined}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Legend */}
        {!isLoading && (buyPoints.length > 0 || sellPoints.length > 0) && (
          <div className="flex gap-4 px-6 pt-2 pb-1 text-xs text-muted-foreground">
            {buyPoints.length > 0 && (
              <span className="flex items-center gap-1.5">
                <span className="inline-block h-2.5 w-2.5 rounded-full bg-profit" />
                Buy
              </span>
            )}
            {sellPoints.length > 0 && (
              <span className="flex items-center gap-1.5">
                <span className="inline-block h-2.5 w-2.5 rounded-full bg-loss" />
                Sell
              </span>
            )}
          </div>
        )}

        {/* Trades table */}
        <div className="mt-4 border-t px-6 pt-4 pb-8">
          <h3 className="mb-3 text-sm font-medium">Trades</h3>
          {isLoading ? (
            <div className="space-y-2">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-8 w-full rounded" />
              ))}
            </div>
          ) : (data?.trades ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No trades recorded for {ticker}.</p>
          ) : (
            <DataTable
              columns={drawerTradesColumns}
              data={data!.trades}
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
