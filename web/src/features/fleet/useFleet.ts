import { useEffect, useState } from "react";
import { getRuntime } from "@/app/runtime";
import type { DeviceView } from "@/fleet/store";

/**
 * A device's live state from the fleet store, sampled a few times a second — often enough to feel
 * live, rarely enough that a panel showing it costs nothing while 10,000 devices stream in.
 */
export function useFleetDevice(id: string | null, intervalMs = 250): DeviceView | null {
  const [view, setView] = useState<DeviceView | null>(() =>
    id ? (getRuntime()?.fleet.viewOf(id) ?? null) : null,
  );
  useEffect(() => {
    if (!id) {
      setView(null);
      return;
    }
    let lastVersion = -1;
    const sample = () => {
      const fleet = getRuntime()?.fleet;
      if (!fleet || fleet.version === lastVersion) return;
      lastVersion = fleet.version;
      const next = fleet.viewOf(id);
      setView((current) =>
        current && next && current.recordedAt === next.recordedAt && current.zoneId === next.zoneId
          ? current
          : next,
      );
    };
    sample();
    const timer = setInterval(sample, intervalMs);
    return () => clearInterval(timer);
  }, [id, intervalMs]);
  return view;
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
