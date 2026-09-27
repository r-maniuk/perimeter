import { useEffect, useLayoutEffect, useState } from "react";
import { getRuntime } from "@/app/runtime";
import type { DeviceView } from "@/fleet/store";

function sameView(a: DeviceView | null, b: DeviceView | null): boolean {
  if (a === null || b === null) return a === b;
  return a.id === b.id && a.recordedAt === b.recordedAt && a.zoneId === b.zoneId;
}

/**
 * A device's live state from the fleet store, sampled a few times a second — often enough to feel
 * live, rarely enough that a panel showing it costs nothing while 10,000 devices stream in.
 *
 * Samples are kept with the id they belong to: when the pointer moves on to another device, the
 * previous one's state is never shown under the new one, even when both last reported at the same
 * moment from outside any zone.
 */
export function useFleetDevice(id: string | null, intervalMs = 250): DeviceView | null {
  const [sampled, setSampled] = useState<{ id: string | null; view: DeviceView | null }>(() => ({
    id,
    view: id ? (getRuntime()?.fleet.viewOf(id) ?? null) : null,
  }));
  // Sampled before paint, so switching devices shows the new one in the very first frame.
  useLayoutEffect(() => {
    if (!id) return;
    let lastVersion = -1;
    const sample = () => {
      const fleet = getRuntime()?.fleet;
      if (!fleet || fleet.version === lastVersion) return;
      lastVersion = fleet.version;
      const next = fleet.viewOf(id);
      setSampled((current) =>
        current.id === id && sameView(current.view, next) ? current : { id, view: next },
      );
    };
    sample();
    const timer = setInterval(sample, intervalMs);
    return () => clearInterval(timer);
  }, [id, intervalMs]);
  return sampled.id === id ? sampled.view : null;
}

/** Server time now (device ages are measured on the server's clock), ticking every second. */
export function useServerNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => getRuntime()?.live.clock.now() ?? Date.now());
  useEffect(() => {
    const timer = setInterval(
      () => setNow(getRuntime()?.live.clock.now() ?? Date.now()),
      intervalMs,
    );
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
