"use client";

import { DataTable } from "@/components/ui/data-table";
import { useDashboard } from "@/lib/api/queries";
import { PositionsTableSkeleton } from "./positions-table-skeleton";
import { positionsColumns, type PositionRow } from "./columns";

export function PositionsTable() {
  const { data: dashboardData, isLoading, isError, error } = useDashboard();

  if (isError) throw error;
  if (isLoading || !dashboardData) return <PositionsTableSkeleton />;

  const data = dashboardData.portfolio;

  const rows: PositionRow[] = Object.entries(data.positions).map(
    ([ticker, pos]) => ({ ...pos, ticker }),
  );

  return (
    <DataTable
      columns={positionsColumns}
      data={rows}
      filters={[
        {
          type: "search",
          columnId: "ticker",
          placeholder: "Filter ticker…",
        },
      ]}
    />
  );
}
