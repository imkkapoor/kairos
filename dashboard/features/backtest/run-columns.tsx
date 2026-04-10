"use client";

import { ColumnDef, FilterFn } from "@tanstack/react-table";
import { selectFilterFn } from "@/components/ui/data-table";
import type { BacktestRunInfo } from "@/lib/types/api";

function pct(v: number | null, d = 1) {
  if (v == null) return "—";
  return `${(v * 100).toFixed(d)}%`;
}

function num(v: number | null, d = 2) {
  if (v == null) return "—";
  return v.toFixed(d);
}

// ─── Custom filter functions ──────────────────────────────────────────────────

/** Search within the configs array (case-insensitive substring). */
export const configSearchFilterFn: FilterFn<BacktestRunInfo> = (row, columnId, value: string) => {
  if (!value) return true;
  const configs = row.getValue(columnId) as string[];
  return configs.some((c) => c.toLowerCase().includes(value.toLowerCase()));
};
configSearchFilterFn.autoRemove = (val: unknown) => !val;

/** Multi-select: row must contain ALL selected feature tags. */
export const featuresFilterFn: FilterFn<BacktestRunInfo> = (row, columnId, values: string[]) => {
  if (!values?.length) return true;
  const tags = row.getValue(columnId) as string[];
  return values.every((v) => tags.includes(v));
};
featuresFilterFn.autoRemove = (val: unknown) => !(val as string[])?.length;

// ─── Column definitions ───────────────────────────────────────────────────────

export const runColumns: ColumnDef<BacktestRunInfo>[] = [
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
    filterFn: configSearchFilterFn,
    cell: ({ getValue }) => (
      <span className="font-mono text-xs">{getValue<string[]>()[0] ?? "—"}</span>
    ),
  },
  {
    accessorKey: "capital_mode",
    header: "Capital Mode",
    enableSorting: true,
    filterFn: selectFilterFn as FilterFn<BacktestRunInfo>,
    cell: ({ getValue }) => {
      const v = getValue<string | null>();
      if (!v) return <span className="text-xs text-muted-foreground">—</span>;
      const label = v === "capital_compounded" ? "Compounded" : "Refresh";
      const color =
        v === "capital_compounded"
          ? "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300"
          : "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300";
      return (
        <span className={`rounded-sm px-1.5 py-0.5 text-[10px] font-medium ${color}`}>
          {label}
        </span>
      );
    },
  },
  {
    accessorKey: "config_origin",
    header: "Origin",
    filterFn: selectFilterFn as FilterFn<BacktestRunInfo>,
    cell: ({ getValue }) => {
      const v = getValue<string | null>() ?? "manual";
      const label = v === "predicted" ? "Predicted" : "Manual";
      const color =
        v === "predicted"
          ? "bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-300"
          : "bg-muted text-muted-foreground";
      return (
        <span className={`rounded-sm px-1.5 py-0.5 text-[10px] font-medium ${color}`}>
          {label}
        </span>
      );
    },
  },
  {
    id: "features",
    header: "Features",
    accessorFn: (row: BacktestRunInfo): string[] => {
      const tags: string[] = [];
      if (row.any_vol_filter) tags.push("VIX");
      if (row.any_soft_cb) tags.push("SCB");
      else if (row.any_circuit_breaker) tags.push("CB");
      if (row.any_crisis_pos_limits) tags.push("CPL");
      return tags;
    },
    filterFn: featuresFilterFn,
    cell: ({ getValue }) => {
      const tags = getValue<string[]>();
      const colorMap: Record<string, string> = {
        VIX: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
        SCB: "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300",
        CB: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
        CPL: "bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300",
      };
      if (tags.length === 0) return <span className="text-xs text-muted-foreground">—</span>;
      return (
        <div className="flex flex-wrap gap-1">
          {tags.map((t) => (
            <span key={t} className={`rounded-sm px-1 py-0.5 text-[10px] font-medium ${colorMap[t] ?? ""}`}>
              {t}
            </span>
          ))}
        </div>
      );
    },
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
    accessorKey: "avg_pnl_pct",
    header: "Avg Return",
    enableSorting: true,
    cell: ({ getValue, row }) => {
      if (row.original.capital_mode === "capital_compounded") {
        return <span className="text-muted-foreground tabular-nums">N/A</span>;
      }
      const v = getValue<number | null>();
      return (
        <span className={v != null && v >= 0 ? "text-profit tabular-nums" : "text-loss tabular-nums"}>
          {pct(v)}
        </span>
      );
    },
  },
  {
    accessorKey: "total_return_pct",
    header: "Net Return",
    enableSorting: true,
    cell: ({ getValue, row }) => {
      if (row.original.capital_mode === "capital_refresh") {
        return <span className="text-muted-foreground tabular-nums">N/A</span>;
      }
      const v = getValue<number | null>();
      return (
        <span className={v != null && v >= 0 ? "text-profit tabular-nums" : "text-loss tabular-nums"}>
          {pct(v)}
        </span>
      );
    },
  },
];

