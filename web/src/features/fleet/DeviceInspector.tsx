import { useQuery } from "@tanstack/react-query";
import { Check, Copy, Crosshair, LocateFixed, Navigation2, Route } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { deviceTrail, getDevice } from "@/api/endpoints";
import { describeError, isApiError } from "@/api/http";
import { PanelFrame } from "@/features/shell/PanelFrame";
import { useZones } from "@/features/zones/useZones";
import {
  compassPoint,
  formatAge,
  formatCoordinate,
  formatDistance,
  formatSpeed,
} from "@/lib/format";
import { distance } from "@/lib/geodesy";
import { mapController } from "@/map/controller";
import { useUi } from "@/state/ui";
import { Button } from "@/ui/Button";
import { cx } from "@/ui/cx";
import { IconButton } from "@/ui/IconButton";
import { Kbd } from "@/ui/Kbd";
import { SectionLabel } from "@/ui/SectionLabel";
import { useFleetDevice, useServerNow } from "./useFleet";

const TRAIL_WINDOWS = [5, 15, 60] as const;
const STALE_S = 60;

export function DeviceInspector({ id, onClose }: { id: string; onClose: () => void }) {
  const live = useFleetDevice(id);
  const now = useServerNow();
  const following = useUi((s) => s.followId === id);
  const [minutes, setMinutes] = useState<(typeof TRAIL_WINDOWS)[number]>(15);
  const { data: zones } = useZones();

  // Devices outside the viewport are not streamed; show their last known state from the API.
  const fallback = useQuery({
    queryKey: ["device", id],
    queryFn: ({ signal }) => getDevice(id, signal),
    enabled: live === null,
    staleTime: 5_000,
  });
  const device = live
    ? {
        lat: live.lat,
        lon: live.lon,
        recordedAt: live.recordedAt,
        speedMps: live.speedMps,
        headingDeg: live.headingDeg,
        zoneId: live.zoneId,
        moving: live.moving,
      }
    : fallback.data
      ? {
          lat: fallback.data.lat,
          lon: fallback.data.lon,
          recordedAt: fallback.data.recordedAt ?? 0,
          speedMps: fallback.data.speedMps,
          headingDeg: fallback.data.headingDeg,
          zoneId: null,
          moving: (fallback.data.speedMps ?? 0) >= 0.5,
        }
      : null;

  const trail = useQuery({
    queryKey: ["trail", id, minutes],
    queryFn: ({ signal }) => deviceTrail(id, minutes, signal),
    staleTime: 15_000,
  });

  useEffect(() => {
    if (trail.data) mapController.showTrail(id, trail.data.coordinates);
  }, [trail.data, id]);
  useEffect(() => () => mapController.clearTrail(), []);

  const zone = device?.zoneId ? zones?.find((z) => z.id === device.zoneId) : undefined;
  const ageS = device ? (now - device.recordedAt) / 1000 : 0;
  const stale = ageS > STALE_S;
  const status = !device ? "Unknown" : stale ? "Stale" : device.moving ? "Moving" : "Stationary";
  const trailLength = trail.data ? pathLength(trail.data.coordinates) : 0;

  return (
    <PanelFrame
      title={<span className="font-mono">{id}</span>}
      subtitle={
        <span className="flex items-center gap-1.5">
          <span
            className={cx(
              "size-1.5 rounded-full",
              status === "Moving" ? "bg-enter" : status === "Stale" ? "bg-warning" : "bg-muted",
            )}
          />
          {status}
          {device && ` · updated ${formatAge(now - device.recordedAt)}`}
        </span>
      }
      onClose={onClose}
      closeLabel="Close device"
      bodyClassName="px-5 pb-5"
      actions={
        device ? (
          <IconButton
            label="Centre on device"
            size="sm"
            side="bottom"
            onClick={() => useUi.getState().flyTo(device.lat, device.lon, 16)}
          >
            <Crosshair className="size-4" />
          </IconButton>
        ) : undefined
      }
    >
      {!device && fallback.isError && (
        <p className="mt-2 rounded-xl bg-surface-2 px-3 py-3 text-[12.5px] text-muted">
          {isApiError(fallback.error, 404)
            ? "This device has not reported recently."
            : describeError(fallback.error)}
        </p>
      )}
      {!device && fallback.isPending && (
        <div className="mt-2 h-24 animate-pulse rounded-2xl bg-surface-2" />
      )}
      {device && (
        <>
          <div className="mt-1 grid grid-cols-2 gap-2">
            <Tile label="Speed">
              <span className="font-semibold text-[22px] text-ink leading-none tracking-[-0.02em]">
                {device.speedMps === null ? "–" : formatSpeed(device.speedMps).replace(" km/h", "")}
              </span>
              {device.speedMps !== null && (
                <span className="ml-1 text-[12px] text-muted">km/h</span>
              )}
            </Tile>
            <Tile label="Heading">
              {device.headingDeg === null ? (
                <span className="font-semibold text-[22px] text-ink leading-none">–</span>
              ) : (
                <span className="flex items-center gap-2">
                  <Navigation2
                    className="size-5 fill-accent text-accent transition-transform duration-500"
                    style={{ transform: `rotate(${device.headingDeg}deg)` }}
                    aria-hidden="true"
                  />
                  <span className="font-semibold text-[22px] text-ink leading-none tracking-[-0.02em]">
                    {compassPoint(device.headingDeg)}
                  </span>
                  <span className="text-[12px] text-muted tabular-nums">
                    {Math.round(device.headingDeg)}°
                  </span>
                </span>
              )}
            </Tile>
          </div>
          <div className="mt-2 rounded-2xl bg-surface-2/70 px-3.5 py-3 ring-1 ring-line ring-inset">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[12px] text-muted">Zone</span>
              {zone ? (
                <button
                  type="button"
                  onClick={() => useUi.getState().select({ kind: "zone", id: zone.id })}
                  className="flex items-center gap-1.5 rounded-md px-1.5 py-0.5 font-medium text-[13px] text-ink hover:bg-surface-3"
                >
                  <span className="size-2.5 rounded-full" style={{ background: zone.color }} />
                  {zone.name}
                </button>
              ) : (
                <span className="text-[13px] text-ink-2">Outside all zones</span>
              )}
            </div>
            <div className="mt-2.5 flex items-center justify-between gap-2 border-line border-t pt-2.5">
              <span className="text-[12px] text-muted">Position</span>
              <CopyPosition lat={device.lat} lon={device.lon} />
            </div>
          </div>
          <div className="mt-3 flex gap-2">
            <Button
              variant={following ? "primary" : "secondary"}
              className="flex-1"
              icon={<LocateFixed className="size-4" />}
              aria-pressed={following}
              onClick={() => useUi.getState().follow(following ? null : id)}
            >
              {following ? "Following" : "Follow"}
              <Kbd tone={following ? "inverse" : "default"}>L</Kbd>
            </Button>
          </div>
        </>
      )}

      <SectionLabel
        aside={
          <div
            role="radiogroup"
            aria-label="Trail length"
            className="flex gap-0.5 rounded-lg bg-surface-3/60 p-0.5"
          >
            {TRAIL_WINDOWS.map((w) => (
              // biome-ignore lint/a11y/useSemanticElements: segmented control built from buttons.
              <button
                key={w}
                type="button"
                role="radio"
                aria-checked={minutes === w}
                onClick={() => setMinutes(w)}
                className={cx(
                  "h-6 rounded-md px-2 font-medium text-[11px] transition-colors",
                  minutes === w
                    ? "bg-surface-solid text-ink shadow-[0_1px_2px_rgb(0_0_0/0.12)]"
                    : "text-muted hover:text-ink",
                )}
              >
                {w === 60 ? "1 h" : `${w} min`}
              </button>
            ))}
          </div>
        }
      >
        Trail
      </SectionLabel>
      <div className="flex items-center gap-3 rounded-2xl bg-surface-2/70 px-3.5 py-3 ring-1 ring-line ring-inset">
        <span className="flex size-9 items-center justify-center rounded-xl bg-accent-soft text-accent">
          <Route className="size-4" />
        </span>
        <div className="min-w-0 flex-1 text-[12.5px]">
          {trail.isPending ? (
            <span className="text-muted">Loading the last {minutes} minutes…</span>
          ) : trail.isError ? (
            <span className="text-critical">{describeError(trail.error)}</span>
          ) : trail.data.coordinates.length < 2 ? (
            <span className="text-muted">No movement in the last {minutes} minutes.</span>
          ) : (
            <>
              <span className="block font-medium text-ink">
                {formatDistance(trailLength)} travelled
              </span>
              <span className="text-muted">
                {trail.data.complete
                  ? `${trail.data.coordinates.length} reports in the last ${minutes} min`
                  : `latest ${trail.data.coordinates.length} reports (trail shortened)`}
              </span>
            </>
          )}
        </div>
      </div>
    </PanelFrame>
  );
}

function pathLength(coordinates: [number, number][]): number {
  let total = 0;
  for (let i = 1; i < coordinates.length; i++) {
    const [lon1, lat1] = coordinates[i - 1] as [number, number];
    const [lon2, lat2] = coordinates[i] as [number, number];
    total += distance({ lat: lat1, lon: lon1 }, { lat: lat2, lon: lon2 });
  }
  return total;
}

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-2xl bg-surface-2/70 px-3.5 py-3 ring-1 ring-line ring-inset">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className="mt-1.5 flex items-baseline">{children}</div>
    </div>
  );
}

function CopyPosition({ lat, lon }: { lat: number; lon: number }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label="Copy position"
      onClick={() => {
        void navigator.clipboard?.writeText(`${lat.toFixed(6)}, ${lon.toFixed(6)}`).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1_400);
        });
      }}
      className="flex items-center gap-1.5 rounded-md px-1.5 py-0.5 font-mono text-[11.5px] text-ink-2 hover:bg-surface-3"
    >
      {formatCoordinate(lat, lon)}
      {copied ? (
        <Check className="size-3.5 text-enter" />
      ) : (
        <Copy className="size-3.5 text-muted" />
      )}
    </button>
  );
}
