"use client";

import { useRouter } from "next/navigation";
import { useBacktestRuns } from "@/lib/api/queries";
import { DataTable, type FilterDef } from "@/components/ui/data-table";
import { runColumns } from "./run-columns";
import type { BacktestRunInfo } from "@/lib/types/api";

const RUN_FILTERS: FilterDef[] = [
  {
    type: "search",
    columnId: "configs",
    placeholder: "Search config…",
  },
  {
    type: "select",
    columnId: "capital_mode",
    label: "Capital Mode",
    options: [
      { label: "Refresh", value: "capital_refresh" },
      { label: "Compounded", value: "capital_compounded" },
    ],
  },
  {
    type: "multi-select",
    columnId: "features",
    label: "Features",
    options: [
      { label: "VIX", value: "VIX" },
      { label: "SCB", value: "SCB" },
      { label: "CB",  value: "CB" },
      { label: "CPL", value: "CPL" },
    ],
  },
  {
    type: "select",
    columnId: "config_origin",
    label: "Origin",
    options: [
      { label: "Manual",    value: "manual" },
      { label: "Predicted", value: "predicted" },
    ],
  },
];

export function BacktestView() {
  const router = useRouter();
  const { data, isLoading, isError, error } = useBacktestRuns();

  if (isError) throw error;

  if (isLoading || !data) {
    return <div className="h-48 animate-pulse rounded-lg bg-muted" />;
  }

  if (data.runs.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No backtest runs found. Run <code className="text-xs">make backtest</code> to generate results.
      </p>
    );
  }

  const currency = data.runs[0]?.currency;
  const handleRowClick = (run: BacktestRunInfo) => {
    router.push(`/backtest/${run.run_id}`);
  };

  return (
    <div className="space-y-2">
      {currency && (
        <p className="text-xs text-muted-foreground">
          Portfolio currency: <span className="font-mono font-medium">{currency}</span>
        </p>
      )}
      <DataTable
        columns={runColumns}
        data={data.runs}
        filters={RUN_FILTERS}
        onRowClick={handleRowClick}
      />
    </div>
  );
}

