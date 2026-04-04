"use client";

import { ColumnDef, FilterFn } from "@tanstack/react-table";

import { selectFilterFn } from "@/components/ui/data-table";
import type { Trade } from "@/lib/types/api";

export const tradesColumns: ColumnDef<Trade>[] = [
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
  { accessorKey: "ticker", header: "Ticker", enableSorting: false },
  {
    accessorKey: "side",
    header: "Action",
    enableSorting: false,
    filterFn: selectFilterFn as FilterFn<Trade>,
    cell: ({ row }) => {
      const side: string = row.getValue("side");
      return (
        <span
          className={
            side === "buy"
              ? "font-semibold text-profit"
              : "font-semibold text-loss"
          }
        >
          {side.toUpperCase()}
        </span>
      );
    },
  },
  {
    accessorKey: "quantity",
    header: "Qty",
    enableSorting: true,
  },
  {
    accessorKey: "fill_price",
    header: "Fill Price",
    enableSorting: true,
    cell: ({ row }) => {
      const v = row.getValue("fill_price") as number | null | undefined;
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
  { accessorKey: "status", header: "Status", enableSorting: false },
];
