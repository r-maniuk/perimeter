import { CircleDashed, CirclePlus, Maximize2, RefreshCw } from "lucide-react";
import { m } from "motion/react";
import { describeError } from "@/api/http";
import type { Zone } from "@/api/schemas";
import { PanelFrame } from "@/features/shell/PanelFrame";
import type { PanelProps } from "@/features/shell/panels";
import { formatCount, formatDistance, formatDuration } from "@/lib/format";
import { mapController } from "@/map/controller";
import { useLive } from "@/state/live";
import { useUi } from "@/state/ui";
import { Button } from "@/ui/Button";
import { cx } from "@/ui/cx";
import { Empty } from "@/ui/Empty";
import { IconButton } from "@/ui/IconButton";
import { isDraft } from "./model";
import { useZones } from "./useZones";

export function ZonesPanel({ onClose, titleId }: PanelProps) {
  const { data: zones, isPending, isError, error, refetch } = useZones();
  const inside = zones?.reduce((sum, z) => sum + (z.occupancy ?? 0), 0) ?? 0;

  return (
    <PanelFrame
      title="Zones"
      titleId={titleId}
      subtitle={
        zones
          ? `${zones.length} ${zones.length === 1 ? "zone" : "zones"} · ${formatCount(inside)} devices inside`
          : "Loading…"
      }
      onClose={onClose}
      actions={
        <>
          {zones && zones.length > 0 && (
            <IconButton
              label="Show all zones"
              size="sm"
              side="bottom"
              onClick={() => mapController.fitZones(zones)}
            >
              <Maximize2 className="size-4" />
            </IconButton>
          )}
          <Button
            size="sm"
            variant="primary"
            icon={<CirclePlus className="size-3.5" />}
            onClick={() => useUi.getState().setDrawing(true)}
          >
            Draw
          </Button>
        </>
      }
    >
      {isPending && <ZoneSkeleton />}
      {isError && (
        <Empty
          icon={<RefreshCw className="size-5" />}
          title="Zones didn't load"
          action={
            <Button size="sm" onClick={() => void refetch()}>
              Try again
            </Button>
          }
        >
          {describeError(error)}
        </Empty>
      )}
      {zones && zones.length === 0 && (
        <Empty
          icon={<CircleDashed className="size-5" />}
          title="No zones yet"
          action={
            <Button
              size="sm"
              variant="primary"
              icon={<CirclePlus className="size-3.5" />}
              onClick={() => useUi.getState().setDrawing(true)}
            >
              Draw a zone
            </Button>
          }
        >
          Press and drag on the map from a centre point. Devices crossing its edge raise alerts.
        </Empty>
      )}
      {zones && zones.length > 0 && (
        <ul className="px-2 pb-3">
          {zones.map((zone, index) => (
            <ZoneRow key={zone.id} zone={zone} index={index} />
          ))}
        </ul>
      )}
    </PanelFrame>
  );
}

function ZoneRow({ zone, index }: { zone: Zone; index: number }) {
  const selected = useUi((s) => s.selection?.kind === "zone" && s.selection.id === zone.id);
  const reporting = useLive((s) => s.reporting[zone.id] ?? 0);
  const draft = isDraft(zone.id);

  return (
    <m.li
      layout="position"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0, transition: { delay: Math.min(index, 12) * 0.025 } }}
    >
      <button
        type="button"
        aria-current={selected ? "true" : undefined}
        onClick={() => {
          useUi.getState().select({ kind: "zone", id: zone.id });
          mapController.fitZones([zone]);
        }}
        onPointerEnter={() => mapController.hoverZone(zone.id)}
        onPointerLeave={() => mapController.hoverZone(null)}
        className={cx(
          "group flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors",
          selected ? "bg-accent-soft" : "hover:bg-surface-3/70",
        )}
      >
        <span className="relative flex size-9 shrink-0 items-center justify-center">
          <span
            className={cx(
              "absolute inset-0 rounded-full opacity-20",
              reporting > 0 && zone.is_active && "animate-halo",
            )}
            style={{ background: zone.color }}
          />
          <span
            className="size-3.5 rounded-full ring-2 ring-surface-solid"
            style={{
              background: zone.is_active ? zone.color : "transparent",
              boxShadow: `inset 0 0 0 2px ${zone.color}`,
            }}
          />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate font-medium text-[13.5px] text-ink">{zone.name}</span>
            {!zone.is_active && (
              <span className="rounded bg-surface-3 px-1 py-px font-medium text-[10px] text-muted uppercase tracking-wide">
                Paused
              </span>
            )}
            {draft && <span className="text-[11px] text-muted">Saving…</span>}
          </span>
          <span className="mt-0.5 block truncate text-[12px] text-muted">
            {formatDistance(zone.radius_m)} radius
            {zone.dwell_s ? ` · dwell ${formatDuration(zone.dwell_s)}` : ""}
            {!zone.notify_enter && !zone.notify_exit && !zone.dwell_s ? " · silent" : ""}
          </span>
        </span>
        <span className="text-right">
          <span className="block font-semibold text-[15px] text-ink tabular-nums leading-none">
            {formatCount(zone.occupancy ?? 0)}
          </span>
          <span className="mt-1 block text-[10.5px] text-muted">
            {reporting > 0 ? `${reporting} live` : "inside"}
          </span>
        </span>
      </button>
    </m.li>
  );
}

function ZoneSkeleton() {
  return (
    <ul className="px-4 pt-1" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <li key={i} className="flex items-center gap-3 py-3">
          <span className="size-9 animate-pulse rounded-full bg-surface-3" />
          <span className="flex-1 space-y-2">
            <span className="block h-3 w-32 animate-pulse rounded bg-surface-3" />
            <span className="block h-2.5 w-20 animate-pulse rounded bg-surface-3" />
          </span>
        </li>
      ))}
    </ul>
  );
}
