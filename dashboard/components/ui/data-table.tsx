"use client";

import * as React from "react";
import {
  ColumnDef,
  ColumnFiltersState,
  FilterFn,
  SortingState,
  Table as TanstackTable,
  flexRender,
  getCoreRowModel,
  getFacetedRowModel,
  getFacetedUniqueValues,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { Check, ChevronsUpDown, Filter, X } from "lucide-react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

// ─── SortIcon ─────────────────────────────────────────────────────────────────

function SortIcon({ sorted }: { sorted: false | "asc" | "desc" }) {
  return (
    <span className="relative inline-flex h-3 w-3 shrink-0">
      <ChevronsUpDown className="h-3 w-3 text-muted-foreground" />
      {sorted && (
        <ChevronsUpDown
          className="absolute inset-0 h-3 w-3 text-white"
          style={{
            clipPath: sorted === "asc" ? "inset(0 0 50% 0)" : "inset(50% 0 0 0)",
          }}
        />
      )}
    </span>
  );
}

// ─── Filter definition types ──────────────────────────────────────────────────

export type SearchFilterDef = {
  type: "search";
  columnId: string;
  placeholder?: string;
};

export type SelectFilterDef = {
  type: "select";
  columnId: string;
  /** Human-readable name shown in the filter popover and active filter chips. */
  label?: string;
  /** Label for the "show all" option. Defaults to "All". */
  allLabel?: string;
  options: Array<{ label: string; value: string; className?: string }>;
};

export type MultiSelectFilterDef = {
  type: "multi-select";
  columnId: string;
  /** Human-readable name shown in the filter popover and active filter chips. */
  label?: string;
  options: Array<{ label: string; value: string; className?: string }>;
};

export type FilterDef = SearchFilterDef | SelectFilterDef | MultiSelectFilterDef;

// ─── Built-in filterFns ───────────────────────────────────────────────────────
// Exported so column defs can reference them directly: filterFn: selectFilterFn

export const selectFilterFn: FilterFn<unknown> = (row, columnId, value: string) =>
  !value ? true : row.getValue(columnId) === value;
selectFilterFn.autoRemove = (val: unknown) => !val;

export const multiSelectFilterFn: FilterFn<unknown> = (
  row,
  columnId,
  values: string[],
) => (!values?.length ? true : values.includes(row.getValue(columnId) as string));
multiSelectFilterFn.autoRemove = (val: unknown) => !(val as string[])?.length;

// ─── FilterPopoverPanel ───────────────────────────────────────────────────────

function FilterPopoverPanel<TData>({
  filters,
  table,
}: {
  filters: (SelectFilterDef | MultiSelectFilterDef)[];
  table: TanstackTable<TData>;
}) {
  return (
    <div className="w-52 space-y-3">
      {filters.map((f, i) => {
        const col = table.getColumn(f.columnId);
        const heading = f.label ?? f.columnId;
        return (
          <div key={f.columnId} className={i > 0 ? "border-t pt-3" : ""}>
            <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              {heading}
            </p>
            <div className="space-y-0.5">
              {f.options.map((opt) => {
                const isMulti = f.type === "multi-select";
                const active = isMulti
                  ? ((col?.getFilterValue() as string[]) ?? []).includes(opt.value)
                  : (col?.getFilterValue() as string) === opt.value;
                return (
                  <button
                    key={opt.value}
                    onClick={() => {
                      if (isMulti) {
                        const cur = (col?.getFilterValue() as string[]) ?? [];
                        const next = active
                          ? cur.filter((v) => v !== opt.value)
                          : [...cur, opt.value];
                        col?.setFilterValue(next.length ? next : undefined);
                      } else {
                        col?.setFilterValue(active ? undefined : opt.value);
                      }
                    }}
                    className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs transition-colors ${
                      active
                        ? "bg-muted font-medium text-foreground"
                        : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                    }`}
                  >
                    <span
                      className={`flex h-3.5 w-3.5 shrink-0 items-center justify-center border transition-colors ${
                        isMulti ? "rounded" : "rounded-full"
                      } ${
                        active
                          ? "border-foreground bg-foreground"
                          : "border-muted-foreground/50"
                      }`}
                    >
                      {active && isMulti && (
                        <Check className="h-2 w-2 text-background" strokeWidth={3} />
                      )}
                      {active && !isMulti && (
                        <span className="h-1.5 w-1.5 rounded-full bg-background" />
                      )}
                    </span>
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── ActiveFilterChips ────────────────────────────────────────────────────────

function ActiveFilterChips<TData>({
  filters,
  table,
}: {
  filters: (SelectFilterDef | MultiSelectFilterDef)[];
  table: TanstackTable<TData>;
}) {
  const chips: { key: string; label: string; onRemove: () => void }[] = [];

  for (const f of filters) {
    const col = table.getColumn(f.columnId);
    if (f.type === "select") {
      const val = col?.getFilterValue() as string | undefined;
      if (val) {
        const opt = f.options.find((o) => o.value === val);
        const prefix = f.label ? `${f.label}: ` : "";
        chips.push({
          key: `${f.columnId}:${val}`,
          label: `${prefix}${opt?.label ?? val}`,
          onRemove: () => col?.setFilterValue(undefined),
        });
      }
    } else {
      const vals = (col?.getFilterValue() as string[] | undefined) ?? [];
      for (const v of vals) {
        const opt = f.options.find((o) => o.value === v);
        chips.push({
          key: `${f.columnId}:${v}`,
          label: opt?.label ?? v,
          onRemove: () => {
            const cur = (col?.getFilterValue() as string[]) ?? [];
            const next = cur.filter((x) => x !== v);
            col?.setFilterValue(next.length ? next : undefined);
          },
        });
      }
    }
  }

  if (chips.length === 0) return null;

  return (
    <>
      {chips.map((chip) => (
        <span
          key={chip.key}
          className="inline-flex items-center gap-1 rounded-md border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
        >
          {chip.label}
          <button
            onClick={chip.onRemove}
            className="ml-0.5 rounded transition-colors hover:text-foreground"
            aria-label="Remove filter"
          >
            <X className="h-2.5 w-2.5" />
          </button>
        </span>
      ))}
      {chips.length > 1 && (
        <button
          onClick={() =>
            filters.forEach((f) =>
              table.getColumn(f.columnId)?.setFilterValue(undefined),
            )
          }
          className="text-xs text-muted-foreground underline-offset-2 transition-colors hover:text-foreground hover:underline"
        >
          Clear all
        </button>
      )}
    </>
  );
}

// ─── DataTable ────────────────────────────────────────────────────────────────

interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[];
  data: TData[];
  /**
   * Declarative filter definitions. Each entry renders a control in the toolbar
   * AND adds a clickable filter icon in the matching column header.
   */
  filters?: FilterDef[];
  /** Extra named filterFns available to column defs as strings. */
  filterFns?: Record<string, FilterFn<TData>>;
  /** Optional row click handler. Makes rows appear clickable. */
  onRowClick?: (row: TData) => void;
}

export function DataTable<TData, TValue>({
  columns,
  data,
  filters = [],
  filterFns: extraFilterFns,
  onRowClick,
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = React.useState<ColumnFiltersState>([]);

  const searchFilters = React.useMemo(
    () => filters.filter((f): f is SearchFilterDef => f.type === "search"),
    [filters],
  );
  const nonSearchFilters = React.useMemo(
    () =>
      filters.filter(
        (f): f is SelectFilterDef | MultiSelectFilterDef => f.type !== "search",
      ),
    [filters],
  );

  const table = useReactTable({
    data,
    columns,
    filterFns: {
      select: selectFilterFn as FilterFn<TData>,
      multiSelect: multiSelectFilterFn as FilterFn<TData>,
      ...extraFilterFns,
    },
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getFacetedRowModel: getFacetedRowModel(),
    getFacetedUniqueValues: getFacetedUniqueValues(),
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    state: { sorting, columnFilters },
    initialState: { pagination: { pageSize: 20 } },
  });

  const filterMap = React.useMemo(
    () => new Map(filters.map((f) => [f.columnId, f])),
    [filters],
  );

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      {filters.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {searchFilters.map((f) => (
            <Input
              key={f.columnId}
              placeholder={f.placeholder ?? "Filter…"}
              value={
                (table.getColumn(f.columnId)?.getFilterValue() as string) ?? ""
              }
              onChange={(e) =>
                table
                  .getColumn(f.columnId)
                  ?.setFilterValue(e.target.value || undefined)
              }
              className="h-8 max-w-xs"
            />
          ))}
          <ActiveFilterChips filters={nonSearchFilters} table={table} />
        </div>
      )}

      {/* Table */}
      <div className="overflow-hidden rounded-md border bg-surface">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const filterDef = filterMap.get(header.column.id);
                  const showFilter =
                    filterDef && filterDef.type !== "search";

                  return (
                    <TableHead key={header.id}>
                      <div className="flex items-center">
                        {header.isPlaceholder ? null : (
                          <button
                            onClick={
                              header.column.getCanSort()
                                ? () => {
                                    if (header.column.getIsSorted() === "desc") {
                                      header.column.clearSorting();
                                    } else {
                                      header.column.toggleSorting(
                                        header.column.getIsSorted() === "asc",
                                      );
                                    }
                                  }
                                : undefined
                            }
                            className={`flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors ${
                              header.column.getCanSort()
                                ? "cursor-pointer select-none hover:text-white"
                                : ""
                            } ${header.column.getIsSorted() ? "text-white" : ""}`}
                          >
                            {flexRender(
                              header.column.columnDef.header,
                              header.getContext(),
                            )}
                            {header.column.getCanSort() && (
                              <SortIcon sorted={header.column.getIsSorted()} />
                            )}
                          </button>
                        )}
                        {showFilter && (
                          <Popover>
                            <PopoverTrigger asChild>
                              <button
                                className={`ml-1 rounded p-0.5 transition-colors hover:bg-muted ${
                                  header.column.getIsFiltered()
                                    ? "text-white"
                                    : "text-muted-foreground"
                                }`}
                                aria-label={`Filter ${header.column.id}`}
                              >
                                <Filter className="h-3 w-3" />
                              </button>
                            </PopoverTrigger>
                            <PopoverContent
                              align="start"
                              className="w-auto p-3"
                              sideOffset={8}
                            >
                              <FilterPopoverPanel
                                filters={
                                  [filterDef] as (
                                    | SelectFilterDef
                                    | MultiSelectFilterDef
                                  )[]
                                }
                                table={table}
                              />
                            </PopoverContent>
                          </Popover>
                        )}
                      </div>
                    </TableHead>
                  );
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.length ? (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-state={row.getIsSelected() && "selected"}
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  className={onRowClick ? "cursor-pointer" : ""}
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell
                  colSpan={columns.length}
                  className="h-24 text-center text-muted-foreground"
                >
                  No results.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>{table.getFilteredRowModel().rows.length} row(s) total</span>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => table.previousPage()}
            disabled={!table.getCanPreviousPage()}
          >
            Previous
          </Button>
          <span>
            Page {table.getState().pagination.pageIndex + 1} of {table.getPageCount()}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => table.nextPage()}
            disabled={!table.getCanNextPage()}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}
