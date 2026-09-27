import { useZone } from "@/features/zones/useZones";
import { formatAge, formatHeading, formatSpeed } from "@/lib/format";
import { usePointer } from "@/state/pointer";
import { useFleetDevice, useServerNow } from "./useFleet";

/** Hover card for a device on the map. */
export function DeviceTooltip() {
  const hover = usePointer((s) => s.hover);
  const device = useFleetDevice(hover?.id ?? null, 200);
  const zone = useZone(device?.zoneId);
  const now = useServerNow(1_000);
  if (!hover || !device) return null;
  const flipX = hover.x > window.innerWidth - 240;
  const flipY = hover.y > window.innerHeight - 150;

  return (
    <div
      role="tooltip"
      className="glass pointer-events-none fixed z-40 min-w-[196px] rounded-xl px-3 py-2.5 text-[12px]"
      style={{
        left: flipX ? hover.x - 16 : hover.x + 16,
        top: flipY ? hover.y - 16 : hover.y + 16,
        transform: `translate(${flipX ? "-100%" : "0"}, ${flipY ? "-100%" : "0"})`,
      }}
    >
      <div className="font-mono font-semibold text-[12.5px] text-ink">{device.id}</div>
      <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <dt className="text-muted">Speed</dt>
        <dd className="text-right text-ink tabular-nums">{formatSpeed(device.speedMps)}</dd>
        <dt className="text-muted">Heading</dt>
        <dd className="text-right text-ink tabular-nums">{formatHeading(device.headingDeg)}</dd>
        <dt className="text-muted">Updated</dt>
        <dd className="text-right text-ink tabular-nums">{formatAge(now - device.recordedAt)}</dd>
        {zone && (
          <>
            <dt className="text-muted">Zone</dt>
            <dd className="flex items-center justify-end gap-1.5 truncate text-ink">
              <span className="size-2 shrink-0 rounded-full" style={{ background: zone.color }} />
              {zone.name}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}
