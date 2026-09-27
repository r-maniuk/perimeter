import { describe, expect, it } from "vitest";
import type { OpsFrame } from "@/api/schemas";
import { admission, instances, ownership } from "./model";

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
