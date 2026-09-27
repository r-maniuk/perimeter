import { useInfiniteQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { BellRing, ChevronDown, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { type Alert, listAlerts } from "@/api/endpoints";
import { describeError } from "@/api/http";
import type { AlertKind } from "@/api/schemas";
import { PanelFrame } from "@/features/shell/PanelFrame";
import type { PanelProps } from "@/features/shell/panels";
import { useZones } from "@/features/zones/useZones";
import { formatClock, formatClockShort, formatDay } from "@/lib/format";
import { useAlerts } from "@/state/alerts";
import { useUi } from "@/state/ui";
import { Button } from "@/ui/Button";
import { cx } from "@/ui/cx";
import { Empty } from "@/ui/Empty";
import { Spinner } from "@/ui/Spinner";
import { KIND_ICON } from "./kinds";
import { groupByMinute, mergeAlerts } from "./timeline";

const KINDS: { value: AlertKind | null; label: string }[] = [
  { value: null, label: "All" },
  { value: "enter", label: "Enter" },
  { value: "exit", label: "Exit" },
  { value: "dwell", label: "Dwell" },
];

const VERB: Record<AlertKind, string> = { enter: "entered", exit: "left", dwell: "dwelling in" };
const CHIP: Record<AlertKind, string> = {
  enter: "bg-enter/12 text-enter",
  exit: "bg-exit/12 text-exit",
  dwell: "bg-dwell/12 text-dwell",
};

export function AlertsPanel({ onClose, titleId }: PanelProps) {
  const kind = useUi((s) => s.alertKind);
  const zoneId = useUi((s) => s.alertZone);
  const live = useAlerts((s) => s.live);
  const { data: zones } = useZones();
  const scroller = useRef<HTMLElement>(null);

  const history = useInfiniteQuery({
    queryKey: ["alerts", { kind, zoneId }],
    queryFn: ({ pageParam, signal }) =>
      listAlerts(
        { kind: kind ?? undefined, zoneId: zoneId ?? undefined, cursor: pageParam, limit: 60 },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.nextCursor,
    staleTime: 10_000,
  });

  const rows = useMemo(() => {
    const fetched = history.data?.pages.flatMap((p) => p.items) ?? [];
    return groupByMinute(mergeAlerts(live, fetched, { kind, zoneId }));
  }, [history.data, live, kind, zoneId]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: (i) => (rows[i]?.type === "minute" ? 34 : 56),
    overscan: 12,
    getItemKey: (i) => rows[i]?.key ?? i,
  });
  const items = virtualizer.getVirtualItems();
  const lastIndex = items.at(-1)?.index ?? 0;
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = history;

  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && lastIndex >= rows.length - 8) {
      void fetchNextPage();
    }
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, lastIndex, rows.length]);

  const zoneColor = (id: string | null) => zones?.find((z) => z.id === id)?.color ?? "var(--muted)";

  return (
    <PanelFrame
      title="Alerts"
      titleId={titleId}
      subtitle="Enter, exit and dwell events across your zones"
      onClose={onClose}
      bodyRef={scroller}
      toolbar={
        <div className="flex items-center gap-2">
          <div
            role="radiogroup"
            aria-label="Kind"
            className="flex flex-1 gap-0.5 rounded-xl bg-surface-3/60 p-0.5"
          >
            {KINDS.map((option) => (
              // biome-ignore lint/a11y/useSemanticElements: segmented control built from buttons.
              <button
                key={option.label}
                type="button"
                role="radio"
                aria-checked={kind === option.value}
                onClick={() => useUi.getState().setAlertKind(option.value)}
                className={cx(
                  "h-7 flex-1 rounded-[10px] font-medium text-[12px] transition-colors",
                  kind === option.value
                    ? "bg-surface-solid text-ink shadow-[0_1px_2px_rgb(0_0_0/0.12)]"
                    : "text-muted hover:text-ink",
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
          <div className="relative">
            <select
              aria-label="Zone"
              value={zoneId ?? ""}
              onChange={(e) => useUi.getState().setAlertZone(e.target.value || null)}
              className="h-8 max-w-[132px] appearance-none truncate rounded-xl bg-surface-solid py-0 pr-7 pl-2.5 font-medium text-[12px] text-ink outline-none ring-1 ring-line-strong focus:ring-2 focus:ring-accent"
            >
              <option value="">All zones</option>
              {zones?.map((z) => (
                <option key={z.id} value={z.id}>
                  {z.name}
                </option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute top-2 right-2 size-4 text-muted" />
          </div>
        </div>
      }
    >
      {history.isPending && rows.length === 0 && (
        <div className="flex justify-center py-10 text-muted">
          <Spinner />
        </div>
      )}
      {history.isError && rows.length === 0 && (
        <Empty
          icon={<RefreshCw className="size-5" />}
          title="Alerts didn't load"
          action={
            <Button size="sm" onClick={() => void history.refetch()}>
              Try again
            </Button>
          }
        >
          {describeError(history.error)}
        </Empty>
      )}
      {!history.isPending && rows.length === 0 && !history.isError && (
        <Empty
          icon={<BellRing className="size-5" />}
          title={kind || zoneId ? "Nothing matches" : "No alerts yet"}
        >
          {kind || zoneId
            ? "No alerts for this filter. Try another kind or zone."
            : "Alerts appear here the moment a device enters, leaves or lingers in one of your zones."}
        </Empty>
      )}
      {rows.length > 0 && (
        <div className="relative px-2 pb-3" style={{ height: virtualizer.getTotalSize() + 12 }}>
          {items.map((item) => {
            const row = rows[item.index];
            if (!row) return null;
            return (
              <div
                key={item.key}
                data-index={item.index}
                ref={virtualizer.measureElement}
                className="absolute inset-x-2"
                style={{ transform: `translateY(${item.start}px)` }}
              >
                {row.type === "minute" ? (
                  <div className="flex items-center gap-2 px-2 pt-3 pb-1.5">
                    <span className="font-mono font-semibold text-[11.5px] text-ink tabular-nums">
                      {formatClockShort(row.at)}
                    </span>
                    <span className="text-[11px] text-muted">
                      {formatDay(row.at) === "Today" ? "" : formatDay(row.at)}
                    </span>
                    <span className="h-px flex-1 bg-line" />
                    <span className="text-[11px] text-muted tabular-nums">{row.count}</span>
                  </div>
                ) : (
                  <AlertRow alert={row.alert} color={zoneColor(row.alert.zoneId)} />
                )}
              </div>
            );
          })}
        </div>
      )}
      {isFetchingNextPage && (
        <div className="flex justify-center pb-4 text-muted">
          <Spinner className="size-3.5" />
        </div>
      )}
    </PanelFrame>
  );
}

function AlertRow({ alert, color }: { alert: Alert; color: string }) {
  return (
    <button
      type="button"
      onClick={() => {
        const ui = useUi.getState();
        ui.select({ kind: "device", id: alert.deviceId });
        ui.flyTo(alert.lat, alert.lon, 16);
      }}
      className="group flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-surface-3/70"
    >
      <span
        className={cx(
          "flex size-8 shrink-0 items-center justify-center rounded-xl",
          CHIP[alert.kind],
        )}
      >
        {KIND_ICON[alert.kind]}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] text-ink">
          <span className="font-medium font-mono">{alert.deviceId}</span>{" "}
          <span className="text-ink-2">{VERB[alert.kind]}</span>
        </span>
        <span className="mt-0.5 flex items-center gap-1.5 text-[12px] text-muted">
          <span className="size-2 shrink-0 rounded-full" style={{ background: color }} />
          <span className="truncate">{alert.zoneName}</span>
        </span>
      </span>
      <span className="shrink-0 font-mono text-[11px] text-muted tabular-nums">
        {formatClock(alert.occurredAt)}
      </span>
    </button>
  );
}
