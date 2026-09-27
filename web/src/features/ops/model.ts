/**
 * Shaping `ops` frames (one-second heartbeats of every api and engine process) into what the
 * pipeline view shows: throughput, backpressure, latency, partition ownership, process health.
 */
import type { OpsFrame, ServiceSnapshot } from "@/api/schemas";

export interface Instance {
  service: string;
  instance: string;
  ageS: number;
  loopLagMs: number;
  raw: ServiceSnapshot & Record<string, unknown>;
}

export interface Ownership {
  /** Owner instance per partition index, `null` when no live engine claims it. */
  owners: (string | null)[];
  /** Engines in a stable order (by instance id), with how many partitions each owns. */
  engines: { instance: string; count: number; slot: number }[];
  conflicts: number[];
}

export const PARTITIONS = 16;

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export function instances(frame: OpsFrame, nowS: number): Instance[] {
  return frame.services
    .map((s) => {
      const raw = s as ServiceSnapshot & Record<string, unknown>;
      return {
        service: s.service,
        instance: s.instance,
        ageS: Math.max(0, nowS - num(s.ts ?? nowS)),
        loopLagMs: num(s.loop_lag_p99_ms),
        raw,
      };
    })
    .sort((a, b) => a.service.localeCompare(b.service) || a.instance.localeCompare(b.instance));
}

/** Partition → owner, from each engine's `partitions` list. Two claimants mark a conflict. */
export function ownership(frame: OpsFrame, partitions = PARTITIONS): Ownership {
  const engines = frame.services
    .filter((s) => s.service === "engine")
    .map((s) => ({
      instance: s.instance,
      parts: Array.isArray((s as Record<string, unknown>).partitions)
        ? ((s as Record<string, unknown>).partitions as unknown[])
            .map(Number)
            .filter(Number.isInteger)
        : [],
    }))
    .sort((a, b) => a.instance.localeCompare(b.instance));
  const owners: (string | null)[] = new Array(partitions).fill(null);
  const conflicts = new Set<number>();
  for (const engine of engines) {
    for (const p of engine.parts) {
      if (p < 0 || p >= partitions) continue;
      if (owners[p] !== null && owners[p] !== engine.instance) conflicts.add(p);
      owners[p] = engine.instance;
    }
  }
  return {
    owners,
    engines: engines.map((e, slot) => ({
      instance: e.instance,
      count: owners.filter((o) => o === e.instance).length,
      slot,
    })),
    conflicts: [...conflicts].sort((a, b) => a - b),
  };
}

type Snapshot = ServiceSnapshot & Record<string, unknown>;

/**
 * One number per plotted metric from one `ops` frame, by the heartbeat names of spec §18.2.
 * Rates add up across processes. Backlogs are global (every replica samples the same stream or
 * outbox table), so the freshest-looking, largest sample stands for them; latencies take the
 * slowest process.
 */
export function seriesOf(frame: OpsFrame): Record<string, number> {
  const api = frame.services.filter((s) => s.service === "api") as Snapshot[];
  const engines = frame.services.filter((s) => s.service === "engine") as Snapshot[];
  const sum = (list: Snapshot[], key: string) => list.reduce((t, s) => t + num(s[key]), 0);
  const max = (list: Snapshot[], key: string) => list.reduce((m, s) => Math.max(m, num(s[key])), 0);
  const series: Record<string, number> = {
    ingest: sum(api, "ingest_rate"),
    rejected: sum(api, "ingest_rejected_rate"),
    lag: max(api, "lag"),
    publishP99: max(api, "publish_p99_ms"),
    wsOut: sum(api, "live_out_rate"),
    drops: sum(api, "live_drops_rate"),
    reports: sum(engines, "reports_rate"),
    alerts: sum(engines, "alerts_rate"),
    late: sum(engines, "late_rate"),
    batchP50: max(engines, "batch_p50_ms"),
    batchP99: max(engines, "batch_p99_ms"),
    commitLag: max(engines, "commit_lag_p99_ms"),
    backlog: max(engines, "relay_backlog"),
  };
  for (const s of frame.services) series[`lag:${s.instance}`] = num(s.loop_lag_p99_ms);
  return series;
}

/**
 * The mean of the last `window` one-second samples. Rates of rare events (a few alerts a second)
 * swing between 0 and 5 from one second to the next; the headline number should not.
 */
export function recentMean(values: readonly number[] | undefined, window = 5): number {
  if (!values || values.length === 0) return 0;
  const tail = values.slice(-window);
  return tail.reduce((total, value) => total + value, 0) / tail.length;
}

export type AdmissionState = "open" | "shedding" | "unknown";

/** The pipeline sheds load if any api replica does (each decides on its own view of the lag). */
export function admission(frame: OpsFrame): AdmissionState {
  const states = frame.services
    .filter((s) => s.service === "api")
    .map((s) => String((s as Record<string, unknown>).admission ?? ""));
  if (states.length === 0) return "unknown";
  return states.includes("shedding") ? "shedding" : "open";
}
