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
import { ChevronsUpDown, Filter } from "lucide-react";

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
import { Toggle } from "@/components/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

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
  /** Label for the "show all" option. Defaults to "All". */
  allLabel?: string;
  options: Array<{ label: string; value: string; className?: string }>;
};

export type MultiSelectFilterDef = {
  type: "multi-select";
  columnId: string;
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

// ─── FilterControl ────────────────────────────────────────────────────────────

function FilterControl<TData>({
  filter,
  table,
  orientation = "horizontal",
}: {
  filter: FilterDef;
  table: TanstackTable<TData>;
  orientation?: "horizontal" | "vertical";
}) {
  const column = table.getColumn(filter.columnId);

  if (filter.type === "search") {
    return (
      <Input
        placeholder={filter.placeholder ?? "Filter…"}
        value={(column?.getFilterValue() as string) ?? ""}
        onChange={(e) => column?.setFilterValue(e.target.value || undefined)}
        className="max-w-xs"
      />
    );
  }

  if (filter.type === "select") {
    const current = (column?.getFilterValue() as string) ?? "";
    return (
      <ToggleGroup
        type="single"
        value={current}
        onValueChange={(v) => column?.setFilterValue(v || undefined)}
      >
        <ToggleGroupItem value="">{filter.allLabel ?? "All"}</ToggleGroupItem>
        {filter.options.map((opt) => (
          <ToggleGroupItem key={opt.value} value={opt.value} className={opt.className}>
            {opt.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    );
  }

  if (filter.type === "multi-select") {
    const current = (column?.getFilterValue() as string[]) ?? [];
    return (
      <div
        className={
          orientation === "vertical" ? "flex flex-col gap-1" : "flex flex-wrap gap-1"
        }
      >
        {filter.options.map((opt) => (
          <Toggle
            key={opt.value}
            pressed={current.includes(opt.value)}
            size="sm"
            onPressedChange={(pressed) => {
              const next = pressed
                ? [...current, opt.value]
                : current.filter((v) => v !== opt.value);
              column?.setFilterValue(next.length ? next : undefined);
            }}
            className={opt.className}
          >
            {opt.label}
          </Toggle>
        ))}
      </div>
    );
  }

  return null;
}

// ─── FilterPopover ────────────────────────────────────────────────────────────

function FilterPopover({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      ref={ref}
      className="absolute top-full left-0 z-50 mt-1 min-w-[160px] rounded-md border bg-popover p-2 shadow-md"
    >
      {children}
    </div>
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
}

export function DataTable<TData, TValue>({
  columns,
  data,
  filters = [],
  filterFns: extraFilterFns,
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = React.useState<ColumnFiltersState>([]);
  const [openPopover, setOpenPopover] = React.useState<string | null>(null);

  const filterMap = React.useMemo(
    () => new Map(filters.map((f) => [f.columnId, f])),
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

  return (
    <div className="space-y-3">
      {/* Table */}
      <div className="overflow-hidden rounded-md border bg-surface">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const filterDef = filterMap.get(header.column.id);
                  const isOpen = openPopover === header.column.id;
                  const isFiltered = header.column.getIsFiltered();

                  return (
                    <TableHead key={header.id}>
                      <div className="relative flex items-center">
                        {header.isPlaceholder ? null : (
                          <button
                            onClick={header.column.getCanSort() ? () => {
                              if (header.column.getIsSorted() === "desc") {
                                header.column.clearSorting();
                              } else {
                                header.column.toggleSorting(header.column.getIsSorted() === "asc");
                              }
                            } : undefined}
                            className={`flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors ${
                              header.column.getCanSort()
                                ? "cursor-pointer select-none hover:text-white"
                                : ""
                            } ${header.column.getIsSorted() ? "text-white" : ""}`}
                          >
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            {header.column.getCanSort() && (
                              <SortIcon sorted={header.column.getIsSorted()} />
                            )}
                          </button>
                        )}
                        {filterDef && (
                          <>
                            <button
                              onClick={() => setOpenPopover(isOpen ? null : header.column.id)}
                              className={`ml-auto rounded p-0.5 transition-colors hover:bg-muted ${
                                isFiltered ? "text-white" : "text-muted-foreground"
                              }`}
                              aria-label={`Filter ${header.column.id}`}
                            >
                              <Filter className="h-3 w-3" />
                            </button>
                            <FilterPopover open={isOpen} onClose={() => setOpenPopover(null)}>
                              <FilterControl
                                filter={filterDef}
                                table={table}
                                orientation="vertical"
                              />
                            </FilterPopover>
                          </>
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
                <TableRow key={row.id} data-state={row.getIsSelected() && "selected"}>
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
