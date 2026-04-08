import { ErrorBoundary } from "@/components/error-boundary";
import { MetricsCards } from "@/features/metrics/metrics-cards";
import { PortfolioChart } from "@/features/portfolio/portfolio-chart";
import { PortfolioStats } from "@/features/portfolio/portfolio-stats";
import { PositionsTable } from "@/features/positions/positions-table";

export default function Home() {
  return (
    <div className="space-y-10">
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
    </div>
  );
}
