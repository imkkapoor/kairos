"use client";

import { ErrorBoundary } from "@/components/error-boundary";
import { BacktestView } from "@/features/backtest/backtest-view"

export default function BacktestPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Backtest Runs</h1>
      <ErrorBoundary>
        <BacktestView />
      </ErrorBoundary>
    </div>
  );
}
