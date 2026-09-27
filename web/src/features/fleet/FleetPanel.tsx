import { useVirtualizer } from "@tanstack/react-virtual";
import { Navigation2, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Zone } from "@/api/schemas";
import { getRuntime } from "@/app/runtime";
import { PanelFrame } from "@/features/shell/PanelFrame";
import type { PanelProps } from "@/features/shell/panels";
import { useZones } from "@/features/zones/useZones";
import { inBox } from "@/fleet/store";
import { formatAge, formatCount, formatSpeed } from "@/lib/format";
import { mapController } from "@/map/controller";
import { useLive } from "@/state/live";
import { useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { Empty } from "@/ui/Empty";
import { useServerNow } from "./useFleet";

type Sort = "id" | "speed" | "recent";

interface Row {
  id: string;
  speed: number | null;
  recordedAt: number;
  zoneId: string | null;
  moving: boolean;
  lat: number;
  lon: number;
}

/**
 * Devices on screen — the same set the status bar counts — refreshed once a second while the
 * panel is open.
 */
function useFleetRows(): Row[] {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => {
    let version = -1;
    let bounds = "";
    const sample = () => {
      const fleet = getRuntime()?.fleet;
      const viewport = mapController.viewport();
      if (!fleet || !viewport) return;
      const key = viewport.bbox.join(",");
      if (fleet.version === version && key === bounds) return;
      version = fleet.version;
      bounds = key;
      const [west, south, east, north] = viewport.bbox;
      const next: Row[] = [];
      for (let i = 0; i < fleet.count; i++) {
        const view = fleet.view(i);
        if (!view || !inBox(view.lat, view.lon, west, south, east, north)) continue;
        next.push({
          id: view.id,
          speed: view.speedMps,
          recordedAt: view.recordedAt,
          zoneId: view.zoneId,
          moving: view.moving,
          lat: view.lat,
          lon: view.lon,
        });
      }
      setRows(next);
    };
    sample();
    const timer = setInterval(sample, 1_000);
    return () => clearInterval(timer);
  }, []);
  return rows;
}

const collator = new Intl.Collator("en", { numeric: true });

export function FleetPanel({ onClose, titleId }: PanelProps) {
  const rows = useFleetRows();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("id");
  const moving = useLive((s) => s.movingInView);
  const { data: zones } = useZones();
  const now = useServerNow();
  const scroller = useRef<HTMLElement>(null);
  const zoneById = useMemo(() => new Map((zones ?? []).map((z) => [z.id, z])), [zones]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? rows.filter((r) => r.id.toLowerCase().includes(needle))
      : rows.slice();
    if (sort === "id") filtered.sort((a, b) => collator.compare(a.id, b.id));
    if (sort === "speed") filtered.sort((a, b) => (b.speed ?? -1) - (a.speed ?? -1));
    if (sort === "recent") filtered.sort((a, b) => b.recordedAt - a.recordedAt);
    return filtered;
  }, [rows, query, sort]);

  const inZones = useMemo(() => rows.reduce((n, r) => n + (r.zoneId ? 1 : 0), 0), [rows]);

  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => 48,
    overscan: 16,
    getItemKey: (i) => visible[i]?.id ?? i,
  });

  return (
    <PanelFrame
      title="Fleet"
      titleId={titleId}
      subtitle="Devices on screen, live"
      onClose={onClose}
      bodyRef={scroller}
      toolbar={
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <Figure label="In view" value={formatCount(rows.length)} />
            <Figure label="Moving" value={formatCount(moving)} />
            <Figure label="In zones" value={formatCount(inZones)} />
          </div>
          <div className="flex gap-2">
            <label className="relative flex-1">
              <span className="sr-only">Search devices</span>
              <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-muted" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search device id"
                spellCheck={false}
                className="h-8 w-full rounded-xl bg-surface-solid pr-7 pl-8 font-mono text-[12.5px] text-ink outline-none ring-1 ring-line-strong placeholder:font-sans placeholder:text-muted focus:ring-2 focus:ring-accent"
              />
              {query && (
                <button
                  type="button"
                  aria-label="Clear search"
                  onClick={() => setQuery("")}
                  className="absolute top-1.5 right-1.5 flex size-5 items-center justify-center rounded-md text-muted hover:bg-surface-3"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </label>
            <select
              aria-label="Sort devices"
              value={sort}
              onChange={(e) => setSort(e.target.value as Sort)}
              className="h-8 appearance-none rounded-xl bg-surface-solid px-2.5 font-medium text-[12px] text-ink outline-none ring-1 ring-line-strong focus:ring-2 focus:ring-accent"
            >
              <option value="id">By id</option>
              <option value="speed">Fastest</option>
              <option value="recent">Latest</option>
            </select>
          </div>
        </div>
      }
    >
      {visible.length === 0 ? (
        <Empty
          icon={<Navigation2 className="size-5" />}
          title={query ? "No match" : "No devices in view"}
        >
          {query
            ? `No device in the current view matches “${query}”.`
            : "Move or zoom the map; devices stream in for the area you are looking at."}
        </Empty>
      ) : (
        <div className="relative px-2 pb-3" style={{ height: virtualizer.getTotalSize() + 12 }}>
          {virtualizer.getVirtualItems().map((item) => {
            const row = visible[item.index];
            if (!row) return null;
            return (
              <div
                key={item.key}
                className="absolute inset-x-2"
                style={{ height: item.size, transform: `translateY(${item.start}px)` }}
              >
                <DeviceRow
                  row={row}
                  zone={row.zoneId ? zoneById.get(row.zoneId) : undefined}
                  now={now}
                />
              </div>
            );
          })}
        </div>
      )}
    </PanelFrame>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-surface-2/70 px-3 py-2 ring-1 ring-line ring-inset">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="font-semibold text-[17px] text-ink leading-tight tracking-[-0.01em]">
        {value}
      </div>
    </div>
  );
}

function DeviceRow({ row, zone, now }: { row: Row; zone: Zone | undefined; now: number }) {
  const selected = useUi((s) => s.selection?.kind === "device" && s.selection.id === row.id);
  const stale = now - row.recordedAt > 60_000;
  return (
    <button
      type="button"
      aria-current={selected ? "true" : undefined}
      onClick={() => {
        const ui = useUi.getState();
        ui.select({ kind: "device", id: row.id });
        ui.flyTo(row.lat, row.lon, 16);
      }}
      className={cx(
        "flex h-full w-full items-center gap-3 rounded-xl px-2.5 text-left transition-colors",
        selected ? "bg-accent-soft" : "hover:bg-surface-3/70",
        stale && "opacity-55",
      )}
    >
      <span
        className={cx(
          "size-2.5 shrink-0 rounded-full",
          !zone && (row.moving ? "bg-ink" : "bg-muted/60"),
        )}
        style={zone ? { background: zone.color } : undefined}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[12.5px] text-ink">{row.id}</span>
        <span className="block truncate text-[11.5px] text-muted">
          {zone ? zone.name : row.moving ? "Moving" : "Stationary"}
        </span>
      </span>
      <span className="shrink-0 text-right">
        <span className="block text-[12px] text-ink tabular-nums">{formatSpeed(row.speed)}</span>
        <span className="block text-[11px] text-muted tabular-nums">
          {formatAge(now - row.recordedAt)}
        </span>
      </span>
    </button>
  );
}
