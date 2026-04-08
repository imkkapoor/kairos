import { ErrorBoundary } from "@/components/error-boundary";
import { BacktestDetail } from "@/features/backtest/backtest-detail";

interface Params {
  runId: string;
}

export default async function BacktestDetailPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { runId } = await params;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Run Detail</h1>
      <ErrorBoundary>
        <BacktestDetail runId={runId} />
      </ErrorBoundary>
    </div>
  );
}
