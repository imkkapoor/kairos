"use client";

import { useDashboard } from "@/lib/api/queries";
import { Skeleton } from "@/components/ui/skeleton";

const INITIAL_CAPITAL = 100_000;

export function PortfolioStats() {
  const { data: dashboardData, isLoading, isError, error } = useDashboard();

  if (isError) throw error;
  if (isLoading || !dashboardData) return <Skeleton className="mt-2 h-4 w-80" />;

  const data = dashboardData.portfolio;

  const liveValue = (() => {
    const positionsValue = Object.values(data.positions).reduce((sum, pos) => {
      if (pos.price == null) return sum;
      return sum + pos.price * pos.qty * (pos.fx_rate ?? 1);
    }, 0);
    return data.cash + positionsValue;
  })();

  const pnl = liveValue - INITIAL_CAPITAL;
  const pnlPct = (pnl / INITIAL_CAPITAL) * 100;

  return (
    <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-muted-foreground">
      <span>
        Portfolio value:{" "}
        <span className="font-mono font-medium text-foreground">
          {data.currency}{" "}
          {liveValue.toLocaleString(undefined, { minimumFractionDigits: 2 })}
        </span>
      </span>
      <span>·</span>
      <span>
        Cash:{" "}
        <span className="font-mono font-medium text-foreground">
          {data.cash.toLocaleString(undefined, { minimumFractionDigits: 2 })}
        </span>
      </span>
      <span>·</span>
      <span>
        P&amp;L:{" "}
        <span
          className={`font-mono font-medium ${
            pnl >= 0 ? "text-profit" : "text-loss"
          }`}
        >
          {pnl >= 0 ? "+" : ""}
          {pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}{" "}
          ({pnl >= 0 ? "+" : ""}
          {pnlPct.toFixed(2)}%)
        </span>
      </span>
    </div>
  );
}
