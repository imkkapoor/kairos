import { Skeleton } from "@/components/ui/skeleton";

const COL_WIDTHS = ["w-20", "w-16", "w-20", "w-20", "w-24", "w-20"];

export function PositionsTableSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-9 w-56" />
      <div className="rounded-md border overflow-hidden">
        <div className="border-b px-4 py-3 flex gap-6">
          {COL_WIDTHS.map((w, i) => (
            <Skeleton key={i} className={`h-3.5 ${w}`} />
          ))}
        </div>
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="border-b last:border-0 px-4 py-3 flex gap-6">
            {COL_WIDTHS.map((w, j) => (
              <Skeleton key={j} className={`h-4 ${w}`} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
