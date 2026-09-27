import { describe, expect, it } from "vitest";
import type { OpsFrame } from "@/api/schemas";
import { admission, instances, ownership, recentMean, seriesOf } from "./model";

const frame = (services: Record<string, unknown>[]): OpsFrame =>
  ({ type: "ops", services, ts: 100 }) as unknown as OpsFrame;

describe("ops model", () => {
  it("maps partitions to the engines that own them, in a stable engine order", () => {
    const result = ownership(
      frame([
        { service: "engine", instance: "engine-b", partitions: [8, 9, 10] },
        { service: "engine", instance: "engine-a", partitions: [0, 1, 2, 3, 4, 5, 6, 7] },
        { service: "api", instance: "api-1" },
      ]),
    );
    expect(result.engines).toEqual([
      { instance: "engine-a", count: 8, slot: 0 },
      { instance: "engine-b", count: 3, slot: 1 },
    ]);
    expect(result.owners.slice(6, 12)).toEqual([
      "engine-a",
      "engine-a",
      "engine-b",
      "engine-b",
      "engine-b",
      null,
    ]);
    expect(result.conflicts).toEqual([]);
  });

  it("flags a partition claimed twice during a handover", () => {
    const result = ownership(
      frame([
        { service: "engine", instance: "e1", partitions: [3] },
        { service: "engine", instance: "e2", partitions: [3, 99, -1] },
      ]),
    );
    expect(result.conflicts).toEqual([3]);
    expect(result.owners.filter(Boolean)).toHaveLength(1);
  });

  it("reports shedding when any api replica sheds", () => {
    expect(admission(frame([{ service: "api", instance: "a", admission: "open" }]))).toBe("open");
    expect(
      admission(
        frame([
          { service: "api", instance: "a", admission: "open" },
          { service: "api", instance: "b", admission: "shedding" },
        ]),
      ),
    ).toBe("shedding");
    expect(admission(frame([]))).toBe("unknown");
  });

  it("lists instances with heartbeat age and loop lag", () => {
    const list = instances(
      frame([
        { service: "engine", instance: "e1", ts: 98.5, loop_lag_p99_ms: 2.5 },
        { service: "api", instance: "a1", ts: 99.8, loop_lag_p99_ms: "bad" },
      ]),
      100,
    );
    expect(list.map((i) => [i.instance, i.ageS, i.loopLagMs])).toEqual([
      ["a1", expect.closeTo(0.2, 6), 0],
      ["e1", 1.5, 2.5],
    ]);
  });
});

/** Heartbeats as the running stack publishes them (two api replicas, two engines). */
const live = frame([
  {
    service: "api",
    instance: "a39f164ceed8",
    ts: 1790508502.75,
    loop_lag_p99_ms: 5,
    db_pool_checked_out: 0,
    admission: "open",
    lag: 111,
    drain_rate: 3417.8,
    ingest_rate: 2250,
    ingest_rejected_rate: 0,
    ingest_inflight: 0,
    publish_p99_ms: 8.24,
    sessions: 1,
    live_out_rate: 20,
    live_drops_rate: 0,
    ops_viewers: 1,
  },
  {
    service: "api",
    instance: "a89ddd05fd0b",
    ts: 1790508502.82,
    loop_lag_p99_ms: 7,
    db_pool_checked_out: 1,
    admission: "open",
    lag: 167,
    ingest_rate: 1000,
    ingest_rejected_rate: 2,
    publish_p99_ms: null,
    sessions: 1,
    live_out_rate: 44,
    live_drops_rate: 0.5,
  },
  {
    service: "engine",
    instance: "17f154017e89",
    ts: 1790508502.33,
    loop_lag_p99_ms: 5,
    partitions: [0, 1, 2, 5, 6, 7, 8, 10, 15],
    reports_rate: 1966.6,
    batches_rate: 215,
    batch_p50_ms: 5.7,
    batch_p99_ms: 12,
    commit_lag_p99_ms: 59,
    alerts_rate: 3,
    late_rate: 0.4,
    relay_backlog: 6,
  },
  {
    service: "engine",
    instance: "8cf85e131ad9",
    ts: 1790508502.31,
    loop_lag_p99_ms: 4,
    partitions: [3, 4, 9, 11, 12, 13, 14],
    reports_rate: 1486.3,
    batches_rate: 180,
    batch_p50_ms: 6.4,
    batch_p99_ms: 11,
    commit_lag_p99_ms: 72,
    alerts_rate: 1,
    late_rate: 0,
    relay_backlog: 4,
  },
]);

describe("pipeline series", () => {
  it("adds rates across processes and takes the slowest latency", () => {
    const series = seriesOf(live);
    expect(series.ingest).toBe(3250);
    expect(series.rejected).toBe(2);
    expect(series.reports).toBeCloseTo(3452.9, 6);
    expect(series.alerts).toBe(4);
    expect(series.late).toBeCloseTo(0.4, 6);
    expect(series.wsOut).toBe(64);
    expect(series.drops).toBe(0.5);
    expect(series.batchP50).toBe(6.4);
    expect(series.batchP99).toBe(12);
    expect(series.commitLag).toBe(72);
    expect(series.publishP99).toBe(8.24);
  });

  it("does not add up backlogs every replica samples from the same place", () => {
    const series = seriesOf(live);
    expect(series.lag).toBe(167);
    expect(series.backlog).toBe(6);
  });

  it("keeps each process's event-loop lag, and survives missing or null numbers", () => {
    const series = seriesOf(frame([{ service: "api", instance: "x", publish_p99_ms: null }]));
    expect(series["lag:x"]).toBe(0);
    expect(series.publishP99).toBe(0);
    expect(series.reports).toBe(0);
    expect(seriesOf(live)["lag:a89ddd05fd0b"]).toBe(7);
  });

  it("averages the last few seconds of a rate", () => {
    expect(recentMean([0, 5, 1, 4, 0, 5])).toBe(3);
    expect(recentMean([2, 4], 5)).toBe(3);
    expect(recentMean(undefined)).toBe(0);
    expect(recentMean([])).toBe(0);
  });
});
