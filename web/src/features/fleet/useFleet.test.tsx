// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FleetStore } from "@/fleet/store";
import { useFleetDevice } from "./useFleet";

const runtime = vi.hoisted(() => ({ fleet: null as unknown }));

vi.mock("@/app/runtime", () => ({ getRuntime: () => runtime }));

const T0 = 1_790_000_000_000;

let fleet: FleetStore;

beforeEach(() => {
  vi.useFakeTimers();
  fleet = new FleetStore();
  runtime.fleet = fleet;
  // Two devices of the same frame: same report time, both outside every zone.
  fleet.upsert("veh-a", 52.37, 4.89, T0, 10, 90);
  fleet.upsert("veh-b", 52.36, 4.91, T0, 0, null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("a device's live state", () => {
  it("switches to the next device at once, even when both reported at the same moment", () => {
    const view = renderHook(({ id }) => useFleetDevice(id), {
      initialProps: { id: "veh-a" as string | null },
    });
    expect(view.result.current?.id).toBe("veh-a");
    view.rerender({ id: "veh-b" });
    expect(view.result.current).toMatchObject({ id: "veh-b", lat: 52.36, speedMps: 0 });
    view.rerender({ id: null });
    expect(view.result.current).toBeNull();
  });

  it("follows the device as it reports, and forgets it when it leaves", () => {
    const view = renderHook(() => useFleetDevice("veh-a"));
    act(() => {
      fleet.upsert("veh-a", 52.371, 4.891, T0 + 1_000, 11, 90);
      vi.advanceTimersByTime(250);
    });
    expect(view.result.current).toMatchObject({ lat: 52.371, recordedAt: T0 + 1_000 });
    act(() => {
      fleet.retainCoverage([]);
      vi.advanceTimersByTime(250);
    });
    expect(view.result.current).toBeNull();
  });
});
