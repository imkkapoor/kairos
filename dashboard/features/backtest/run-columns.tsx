"use client";

import { ColumnDef } from "@tanstack/react-table";
import type { BacktestRunInfo } from "@/lib/types/api";

function pct(v: number | null, d = 1) {
  if (v == null) return "—";
  return `${(v * 100).toFixed(d)}%`;
}

function num(v: number | null, d = 2) {
  if (v == null) return "—";
  return v.toFixed(d);
}

function usd(v: number | null) {
  if (v == null) return "—";
  const abs = Math.abs(v);
  const formatted =
    abs >= 1_000_000
      ? `$${(v / 1_000_000).toFixed(2)}M`
      : abs >= 1_000
        ? `$${(v / 1_000).toFixed(1)}k`
        : `$${v.toFixed(0)}`;
  return <span className={v >= 0 ? "text-profit" : "text-loss"}>{formatted}</span>;
}

export const runColumns: ColumnDef<BacktestRunInfo>[] = [
  {
    accessorKey: "run_id",
    header: "Run",
    cell: ({ getValue }) => (
      <span className="font-mono text-xs text-muted-foreground">
        {getValue<string>().slice(0, 8)}…
      </span>
    ),
  },
  {
    accessorKey: "run_at",
    header: "Date",
    enableSorting: true,
    cell: ({ getValue }) => {
      const d = new Date(getValue<string>());
      return (
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {d.toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "numeric" })}{" "}
          {d.toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false })}
        </span>
      );
    },
  },
  {
    accessorKey: "configs",
    header: "Config",
    cell: ({ getValue }) => (
      <span className="font-mono text-xs">{getValue<string[]>()[0] ?? "—"}</span>
    ),
  },
  {
    accessorKey: "total_trades",
    header: "Trades",
    enableSorting: true,
    cell: ({ getValue }) => (
      <span className="tabular-nums">{getValue<number | null>() ?? "—"}</span>
    ),
  },
  {
    accessorKey: "avg_sharpe",
    header: "Avg Sharpe",
    enableSorting: true,
    cell: ({ getValue }) => {
      const v = getValue<number | null>();
      return (
        <span className={v != null && v >= 1 ? "text-profit tabular-nums" : "tabular-nums"}>
          {num(v)}
        </span>
      );
    },
  },
  {
    accessorKey: "avg_cagr",
    header: "Avg CAGR",
    enableSorting: true,
    cell: ({ getValue }) => (
      <span className="tabular-nums">{pct(getValue<number | null>())}</span>
    ),
  },
  {
    accessorKey: "avg_max_dd",
    header: "Avg Max DD",
    enableSorting: true,
    cell: ({ getValue }) => (
      <span className="text-loss tabular-nums">{pct(getValue<number | null>())}</span>
    ),
  },
  {
    accessorKey: "avg_win_rate",
    header: "Win Rate",
    enableSorting: true,
    cell: ({ getValue }) => (
      <span className="tabular-nums">{pct(getValue<number | null>())}</span>
    ),
  },
  {
    accessorKey: "total_windows",
    header: "Windows",
    enableSorting: true,
    cell: ({ getValue }) => (
      <span className="tabular-nums">{getValue<number>()}</span>
    ),
  },
  {
    accessorKey: "total_pnl_usd",
    header: "Total PnL",
    enableSorting: true,
    cell: ({ getValue }) => usd(getValue<number | null>()),
  },
  {
    accessorKey: "avg_pnl_pct",
    header: "PnL %",
    enableSorting: true,
    cell: ({ getValue }) => {
      const v = getValue<number | null>();
      return (
        <span className={v != null && v >= 0 ? "text-profit tabular-nums" : "text-loss tabular-nums"}>
          {pct(v)}
        </span>
      );
    },
  },
  {
    accessorKey: "currency",
    header: "CCY",
    cell: ({ getValue }) => (
      <span className="font-mono text-xs text-muted-foreground">{getValue<string>()}</span>
    ),
  },
];
