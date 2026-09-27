import { describe, expect, it } from "vitest";
import type { Alert } from "@/api/endpoints";
import {
  addAlert,
  expire,
  headline,
  hold,
  LIFETIME_MS,
  MAX_VISIBLE,
  MERGE_WINDOW_MS,
} from "./toasts";

function alert(id: string, overrides: Partial<Alert> = {}): Alert {
  return {
    id,
    kind: "enter",
    deviceId: `veh-${id}`,
    zoneId: "z1",
    zoneName: "Dam Square",
    lat: 52.37,
    lon: 4.89,
    occurredAt: 0,
    ...overrides,
  };
}

describe("toast coalescing", () => {
  it("shows a single alert as a specific sentence", () => {
    const toasts = addAlert([], alert("1"), 1_000);
    expect(toasts).toHaveLength(1);
    expect(headline(toasts[0] as never)).toBe("veh-1 entered Dam Square");
    expect(headline(addAlert([], alert("2", { kind: "exit" }), 0)[0] as never)).toBe(
      "veh-2 left Dam Square",
    );
  });

  it("merges a burst into one counting toast", () => {
    let toasts = addAlert([], alert("1"), 1_000);
    toasts = addAlert(toasts, alert("2", { kind: "exit" }), 1_400);
    toasts = addAlert(toasts, alert("3", { zoneName: "Depot" }), 1_900);
    expect(toasts).toHaveLength(1);
    const [toast] = toasts;
    expect(toast?.kinds).toEqual({ enter: 2, exit: 1, dwell: 0 });
    expect(toast && headline(toast)).toBe("3 alerts in 2 zones");
    expect(toast?.expiresAt).toBe(1_900 + LIFETIME_MS);
  });

  it("counts every alert of a long burst, not only the ones it keeps", () => {
    let toasts = addAlert([], alert("0"), 0);
    for (let k = 1; k < 120; k++) toasts = addAlert(toasts, alert(String(k)), k * 10);
    expect(toasts).toHaveLength(1);
    expect(toasts[0]?.count).toBe(120);
    expect(toasts[0]?.alerts).toHaveLength(50);
    expect(toasts[0]?.kinds.enter).toBe(120);
    expect(headline(toasts[0] as never)).toBe("120 alerts in Dam Square");
  });

  it("starts a new toast after the merge window and keeps at most three", () => {
    let toasts = addAlert([], alert("1"), 0);
    for (let k = 1; k <= 5; k++) {
      toasts = addAlert(toasts, alert(String(k + 1)), k * (MERGE_WINDOW_MS + 1));
    }
    expect(toasts).toHaveLength(MAX_VISIBLE);
    expect(toasts.map((t) => t.alerts[0]?.id)).toEqual(["4", "5", "6"]);
  });

  it("expires on time, later when held", () => {
    const toasts = addAlert([], alert("1"), 0);
    const id = toasts[0]?.id as string;
    expect(expire(toasts, LIFETIME_MS - 1)).toHaveLength(1);
    expect(expire(toasts, LIFETIME_MS)).toHaveLength(0);
    expect(expire(hold(toasts, id, 2_000), LIFETIME_MS + 1_000)).toHaveLength(1);
  });
});
