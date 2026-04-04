"use client";

import { DataTable } from "@/components/ui/data-table";
import { useTrades } from "@/lib/api/queries";
import { TradesTableSkeleton } from "./trades-table-skeleton";
import { tradesColumns } from "./columns";

export function TradesTable() {
  const { data, isLoading, isError, error } = useTrades();

  if (isError) throw error;
  if (isLoading || !data) return <TradesTableSkeleton />;

  return (
    <DataTable
      columns={tradesColumns}
      data={data}
      filters={[
        {
          type: "select",
          columnId: "side",
          options: [
            { label: "Buy", value: "buy", className: "text-profit" },
            { label: "Sell", value: "sell", className: "text-loss" },
          ],
        },
        {
          type: "search",
          columnId: "ticker",
          placeholder: "Filter ticker…",
        },
      ]}
    />
  );
}
