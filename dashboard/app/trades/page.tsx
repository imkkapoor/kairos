import { ErrorBoundary } from "@/components/error-boundary";
import { TradesTable } from "@/features/trades/trades-table";

export default function TradesPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Trades</h1>
      <ErrorBoundary>
        <TradesTable />
      </ErrorBoundary>
    </div>
  );
}
