"use client";

import { ColumnDef } from "@tanstack/react-table";
import { TrendingDown, TrendingUp } from "lucide-react";

import type { Position } from "@/lib/types/api";

export type PositionRow = Position & { ticker: string };

export const positionsColumns: ColumnDef<PositionRow>[] = [
  {
    accessorKey: "ticker",
    header: "Ticker",
    enableSorting: false,
    cell: ({ row }) => {
      const chg = row.original.change_today;
      const pct = row.original.change_today_pct;
      if (chg == null || pct == null) return "—";
      const color = chg >= 0 ? "text-profit" : "text-loss";
      return (
        <span className="flex gap-2">
          <span className={`${color}`}>
            {chg >= 0 ? (
              <TrendingUp className="h-3 w-3" strokeWidth={2.5} />
            ) : (
              <TrendingDown className="h-3 w-3" strokeWidth={2.5} />
            )}
          </span>
          <span>{row.getValue("ticker")}</span>
        </span>
      );
    },
  },
  { accessorKey: "qty", header: "Qty", enableSorting: true },
  {
    accessorKey: "avg_cost",
    header: "Average Cost",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("avg_cost") as number | null | undefined;
      return v != null ? <span className="font-mono">${v.toFixed(2)}</span> : "—";
    },
  },
  {
    accessorKey: "stop_loss",
    header: "Stop Loss",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("stop_loss") as number | null | undefined;
      return v != null ? <span className="font-mono">${v.toFixed(2)}</span> : "—";
    },
  },
  {
    accessorKey: "take_profit",
    header: "Take Profit",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("take_profit") as number | null | undefined;
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
  { accessorKey: "currency", header: "Currency", enableSorting: false },
  {
    accessorKey: "price",
    header: "Price",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("price") as number | null | undefined;
      return v != null ? <span className="font-mono">${v.toFixed(2)}</span> : "—";
    },
  },
  {
    id: "change_today",
    accessorKey: "change_today",
    header: "Change Today",
    enableSorting: true,
    cell: ({ row }) => {
      const chg = row.original.change_today;
      const pct = row.original.change_today_pct;
      if (chg == null || pct == null) return "—";
      const color = chg >= 0 ? "text-profit" : "text-loss";
      const sign = chg >= 0 ? "+" : "";
      return (
        <span className={`font-mono ${color}`}>
          {sign}{chg.toFixed(2)} ({sign}{pct.toFixed(2)}%)
        </span>
      );
    },
  },
  {
    id: "pnl",
    header: "PnL",
    enableSorting: true,
    accessorFn: (row) => {
      if (row.price == null) return null;
      return (row.price - row.avg_cost) * row.qty * (row.fx_rate ?? 1);
    },
    cell: ({ row }) => {
      const price = row.original.price;
      const avgCost = row.original.avg_cost;
      if (price == null || avgCost == null) return "—";
      const pnl = (price - avgCost) * row.original.qty * (row.original.fx_rate ?? 1);
      const pnlPct = ((price - avgCost) / avgCost) * 100;
      const color = pnl >= 0 ? "text-profit" : "text-loss";
      const sign = pnl >= 0 ? "+" : "";
      return (
        <span className={`font-mono ${color}`}>
          {sign}{pnl.toFixed(2)} ({sign}{pnlPct.toFixed(2)}%)
        </span>
      );
    },
  },
];
