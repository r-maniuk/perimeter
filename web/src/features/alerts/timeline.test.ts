import { describe, expect, it } from "vitest";
import type { Alert } from "@/api/endpoints";
import { groupByMinute, matches, mergeAlerts } from "./timeline";

const T = Date.parse("2026-09-26T14:32:00Z");

function alert(id: string, offsetS: number, overrides: Partial<Alert> = {}): Alert {
  return {
    id,
    kind: "enter",
    deviceId: "veh-1",
    zoneId: "z1",
    zoneName: "Dam",
    lat: 0,
    lon: 0,
    occurredAt: T + offsetS * 1000,
    ...overrides,
  };
}

describe("alert timeline", () => {
  it("merges live and history newest first without duplicates", () => {
    const live = [alert("c", 90), alert("b", 30)];
    const history = [alert("b", 30), alert("a", 5)];
    expect(mergeAlerts(live, history, { kind: null, zoneId: null }).map((a) => a.id)).toEqual([
      "c",
      "b",
      "a",
    ]);
  });

  it("applies the filter to live arrivals (history is already filtered by the server)", () => {
    const live = [alert("x", 10, { kind: "exit" }), alert("y", 11, { zoneId: "z2" })];
    expect(mergeAlerts(live, [], { kind: "exit", zoneId: null }).map((a) => a.id)).toEqual(["x"]);
    expect(mergeAlerts(live, [], { kind: null, zoneId: "z2" }).map((a) => a.id)).toEqual(["y"]);
    expect(matches(alert("z", 0), { kind: "dwell", zoneId: null })).toBe(false);
  });

  it("groups by minute with counts", () => {
    const rows = groupByMinute(
      mergeAlerts([], [alert("a", 70), alert("b", 65), alert("c", 10)], {
        kind: null,
        zoneId: null,
      }),
    );
    expect(rows.map((r) => (r.type === "minute" ? `${r.at - T}:${r.count}` : r.key))).toEqual([
      "60000:2",
      "a",
      "b",
      "0:1",
      "c",
    ]);
  });
});
