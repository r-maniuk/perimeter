/**
 * Alert toasts that inform without shouting. A burst (ten devices crossing a boundary in the same
 * second) becomes one toast that counts up, instead of ten stacked cards; at most three are
 * visible and each leaves on its own after a few seconds unless the pointer rests on it.
 */
import type { Alert } from "@/api/endpoints";
import type { AlertKind } from "@/api/schemas";

export interface Toast {
  id: string;
  createdAt: number;
  updatedAt: number;
  /** When it leaves (paused while hovered). */
  expiresAt: number;
  /** Every alert folded into this toast (the list below keeps only the most recent ones). */
  count: number;
  alerts: Alert[];
  kinds: Record<AlertKind, number>;
  zones: string[];
}

/** Alerts arriving while the latest toast is still on screen join it, for up to this long. */
export const MERGE_WINDOW_MS = 8_000;
export const LIFETIME_MS = 5_500;
export const MAX_LIFETIME_MS = 12_000;
export const MAX_VISIBLE = 3;
const KEEP_ALERTS = 50;

let counter = 0;

function emptyKinds(): Record<AlertKind, number> {
  return { enter: 0, exit: 0, dwell: 0 };
}

/** Add `alert` to the stack at time `now`, merging it into a fresh toast when one is open. */
export function addAlert(toasts: readonly Toast[], alert: Alert, now: number): Toast[] {
  const latest = toasts.at(-1);
  if (latest && now - latest.createdAt <= MERGE_WINDOW_MS && latest.expiresAt > now) {
    const merged: Toast = {
      ...latest,
      updatedAt: now,
      // A merge only ever gives the toast more time: while the pointer rests on it, its expiry is
      // held far out, and an alert joining it must not cut that short.
      expiresAt: Math.max(
        latest.expiresAt,
        Math.min(latest.createdAt + MAX_LIFETIME_MS, now + LIFETIME_MS),
      ),
      count: latest.count + 1,
      alerts: [...latest.alerts, alert].slice(-KEEP_ALERTS),
      kinds: { ...latest.kinds, [alert.kind]: latest.kinds[alert.kind] + 1 },
      zones: latest.zones.includes(alert.zoneName)
        ? latest.zones
        : [...latest.zones, alert.zoneName],
    };
    return [...toasts.slice(0, -1), merged];
  }
  counter += 1;
  const toast: Toast = {
    id: `t${counter}`,
    createdAt: now,
    updatedAt: now,
    expiresAt: now + LIFETIME_MS,
    count: 1,
    alerts: [alert],
    kinds: { ...emptyKinds(), [alert.kind]: 1 },
    zones: [alert.zoneName],
  };
  return [...toasts, toast].slice(-MAX_VISIBLE);
}

export function expire(toasts: readonly Toast[], now: number): Toast[] {
  return toasts.filter((t) => t.expiresAt > now);
}

/** Keep a hovered toast on screen: push its expiry out by the time it was held. */
export function hold(toasts: readonly Toast[], id: string, extraMs: number): Toast[] {
  return toasts.map((t) => (t.id === id ? { ...t, expiresAt: t.expiresAt + extraMs } : t));
}

/** One line of copy for a toast: specific for a single alert, a summary for a burst. */
export function headline(toast: Toast): string {
  const total = toast.count;
  const only = toast.alerts[0];
  if (total === 1 && only) {
    const verb =
      only.kind === "enter" ? "entered" : only.kind === "exit" ? "left" : "is dwelling in";
    return `${only.deviceId} ${verb} ${only.zoneName}`;
  }
  const where = toast.zones.length === 1 ? toast.zones[0] : `${toast.zones.length} zones`;
  return `${total} alerts in ${where}`;
}
