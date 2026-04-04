import { Skeleton } from "@/components/ui/skeleton";

export function MetricsCardsSkeleton() {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl bg-surface border border-card-border flex flex-col p-3 gap-2 min-h-[110px]"
        >
          <Skeleton className="h-3 w-16 bg-white/10" />
          <Skeleton className="h-5 w-24 bg-white/10" />
          <Skeleton className="h-3 w-12 bg-white/10" />
          <Skeleton className="mt-auto h-11 w-full bg-white/10" />
        </div>
      ))}
    </div>
  );
}
