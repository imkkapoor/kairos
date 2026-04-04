import { ErrorBoundary } from "@/components/error-boundary";
import { MetricsCards } from "@/features/metrics/metrics-cards";
import { PortfolioChart } from "@/features/portfolio/portfolio-chart";
import { PortfolioStats } from "@/features/portfolio/portfolio-stats";
import { PositionsTable } from "@/features/positions/positions-table";
import { TradesTable } from "@/features/trades/trades-table";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-7xl px-6 py-10 space-y-12">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Kairos Dashboard
        </h1>
        <ErrorBoundary>
          <PortfolioStats />
        </ErrorBoundary>
      </div>

      <ErrorBoundary>
        <MetricsCards />
      </ErrorBoundary>

      <section className="space-y-3">
        <ErrorBoundary>
          <PortfolioChart />
        </ErrorBoundary>
      </section>

      <section className="space-y-3">
        <ErrorBoundary>
          <PositionsTable />
        </ErrorBoundary>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium">Trades</h2>
        <ErrorBoundary>
          <TradesTable />
        </ErrorBoundary>
      </section>
    </main>
  );
}
