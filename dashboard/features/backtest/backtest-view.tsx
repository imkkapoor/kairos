"use client";

import { useRouter } from "next/navigation";
import { useBacktestRuns } from "@/lib/api/queries";
import { DataTable } from "@/components/ui/data-table";
import { runColumns } from "./run-columns";
import type { BacktestRunInfo } from "@/lib/types/api";

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

  const handleRowClick = (run: BacktestRunInfo) => {
    router.push(`/backtest/${run.run_id}`);
  };

  return (
    <DataTable
      columns={runColumns}
      data={data.runs}
      filters={[]}
      onRowClick={handleRowClick}
    />
  );
}
