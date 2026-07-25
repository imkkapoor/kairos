"use client";

import { ColumnDef } from "@tanstack/react-table";
import type { BacktestRun } from "@/lib/types/api";

function pct(v: number | null, decimals = 1) {
  if (v == null) return "—";
  return `${(v * 100).toFixed(decimals)}%`;
}

function num(v: number | null, decimals = 2) {
  if (v == null) return "—";
  return v.toFixed(decimals);
}

function usd(v: number | null) {
  if (v == null) return "—";
  const abs = Math.abs(v);
  const formatted =
    abs >= 1000 ? `$${(v / 1000).toFixed(1)}k` : `$${v.toFixed(0)}`;
  return <span className={v >= 0 ? "text-profit" : "text-loss"}>{formatted}</span>;
}

export const backtestColumns: ColumnDef<BacktestRun>[] = [
  {
    accessorKey: "window_index",
    header: "Win",
    enableSorting: true,
    cell: ({ row }) => (
      <span className="font-mono text-muted-foreground">
        W{row.getValue("window_index")}
      </span>
    ),
  },
  {
    accessorKey: "window_start",
    header: "Period",
    enableSorting: true,
    cell: ({ row }) => {
      const start = (row.getValue("window_start") as string).slice(0, 7);
      const end = (row.original.window_end as string).slice(0, 7);
      return (
        <span className="font-mono text-xs">
          {start} → {end}
        </span>
      );
    },
  },
  {
    accessorKey: "use_vol_filter",
    header: "Vol Filter",
    enableSorting: false,
    cell: ({ row }) => {
      const active = row.getValue("use_vol_filter");
      const avgVix = row.original.avg_vix;
      if (!active) return <span className="text-muted-foreground text-xs">—</span>;
      return (
        <span className="inline-flex items-center gap-1">
          <span className="rounded-sm bg-amber-100 px-1 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
            VIX
          </span>
          {avgVix != null && (
            <span className="text-xs tabular-nums text-muted-foreground">
              {avgVix.toFixed(1)}
            </span>
          )}
        </span>
      );
    },
  },
  {
    accessorKey: "total_trades",
    header: "Trades",
    enableSorting: true,
  },
  {
    accessorKey: "sharpe_ratio",
    header: "Sharpe",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("sharpe_ratio") as number | null;
      if (v == null) return "—";
      return (
        <span className={v >= 1 ? "text-profit" : v < 0 ? "text-loss" : ""}>
          {v.toFixed(2)}
        </span>
      );
    },
  },
  {
    accessorKey: "calmar_ratio",
    header: "Calmar",
    enableSorting: true,
    cell: ({ row }) => num(row.getValue("calmar_ratio")),
  },
  {
    accessorKey: "cagr",
    header: "CAGR",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("cagr") as number | null;
      if (v == null) return "—";
      return (
        <span className={v >= 0 ? "text-profit" : "text-loss"}>
          {pct(v)}
        </span>
      );
    },
  },
  {
    accessorKey: "max_drawdown",
    header: "Max DD",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("max_drawdown") as number | null;
      if (v == null) return "—";
      return <span className="text-loss">{pct(v)}</span>;
    },
  },
  {
    accessorKey: "win_rate",
    header: "Win %",
    enableSorting: true,
    cell: ({ row }) => pct(row.getValue("win_rate")),
  },
  {
    accessorKey: "total_pnl_usd",
    header: ({ table }) => {
      const firstRow = table.getCoreRowModel().rows[0]?.original as BacktestRun | undefined;
      const currency = firstRow?.currency ?? "Portfolio Currency";
      return `P&L (${currency})`;
    },
    enableSorting: true,
    cell: ({ row }) => usd(row.getValue("total_pnl_usd")),
  },
];
