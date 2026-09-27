/** Alert timeline shaping: merge history with live arrivals, then group by minute. */
import type { Alert } from "@/api/endpoints";
import type { AlertKind } from "@/api/schemas";

export interface AlertFilter {
  kind: AlertKind | null;
  zoneId: string | null;
}

export function matches(alert: Alert, filter: AlertFilter): boolean {
  return (
    (!filter.kind || alert.kind === filter.kind) &&
    (!filter.zoneId || alert.zoneId === filter.zoneId)
  );
}

/** Newest first, each alert once (live arrivals overlap the first history page). */
export function mergeAlerts(
  live: readonly Alert[],
  history: readonly Alert[],
  filter: AlertFilter,
) {
  const seen = new Set<string>();
  const merged: Alert[] = [];
  for (const alert of [...live.filter((a) => matches(a, filter)), ...history]) {
    if (seen.has(alert.id)) continue;
    seen.add(alert.id);
    merged.push(alert);
  }
  return merged.sort((a, b) => b.occurredAt - a.occurredAt || (a.id < b.id ? 1 : -1));
}

export type TimelineRow =
  | { type: "minute"; key: string; at: number; count: number }
  | { type: "alert"; key: string; alert: Alert };

/** Flatten into rows with a header per minute (the shape the virtual list renders). */
export function groupByMinute(alerts: readonly Alert[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  let current: { type: "minute"; key: string; at: number; count: number } | null = null;
  for (const alert of alerts) {
    const minute = Math.floor(alert.occurredAt / 60_000) * 60_000;
    if (!current || current.at !== minute) {
      current = { type: "minute", key: `m${minute}`, at: minute, count: 0 };
      rows.push(current);
    }
    current.count += 1;
    rows.push({ type: "alert", key: alert.id, alert });
  }
  return rows;
}
