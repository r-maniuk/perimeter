// @vitest-environment jsdom
import "@/test/dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tooltip } from "radix-ui";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DeviceState } from "@/api/endpoints";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { ZONES_KEY } from "@/features/zones/model";
import { FleetStore } from "@/fleet/store";
import { useUi } from "@/state/ui";
import { DeviceInspector } from "./DeviceInspector";

const api = vi.hoisted(() => ({
  getDevice: vi.fn(),
  deviceTrail: vi.fn(),
  listZones: vi.fn(),
}));
const runtime = vi.hoisted(() => ({
  fleet: null as unknown,
  live: { clock: { now: () => Date.now(), offsetMs: 0 } },
}));

vi.mock("@/api/endpoints", () => api);
vi.mock("@/app/runtime", () => ({ getRuntime: () => runtime }));

const NOW = Date.parse("2026-09-27T09:00:00Z");

function zone(overrides: Partial<Zone>): Zone {
  return {
    id: "z1",
    name: "Dam Square",
    color: "#6d5dfc",
    center: { lat: 52.3731, lon: 4.8926 },
    radius_m: 400,
    is_active: true,
    notify_enter: true,
    notify_exit: true,
    dwell_s: null,
    version: 1,
    created_at: "2026-09-26T10:00:00Z",
    updated_at: "2026-09-26T10:00:00Z",
    occupancy: 3,
    ...overrides,
  };
}

function device(zones: DeviceState["zones"]): DeviceState {
  return {
    id: "veh-7",
    lat: 52.09,
    lon: 5.12,
    recordedAt: NOW - 30_000,
    speedMps: 0,
    headingDeg: null,
    accuracyM: 5,
    zones,
  };
}

function inspect() {
  return render(
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <DeviceInspector id="veh-7" onClose={() => {}} />
      </Tooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  runtime.fleet = new FleetStore();
  api.deviceTrail.mockResolvedValue({ coordinates: [], times: [], since: NOW, complete: true });
  queryClient.setQueryData(ZONES_KEY, [
    zone({ id: "z-depot", name: "Utrecht depot", color: "#3f9a2e" }),
    zone({ id: "z-city", name: "Utrecht" }),
  ]);
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  useUi.getState().select(null);
  for (const mock of Object.values(api)) mock.mockReset();
});

describe("device inspector for a device outside the view", () => {
  it("shows the zones the server says it is in, and opens one on click", async () => {
    api.getDevice.mockResolvedValue(
      device([
        { id: "z-depot", name: "Depot (old name)", color: "#000000", enteredAt: NOW - 60_000 },
        { id: "z-gone", name: "Harbour", color: "#2f7fe0", enteredAt: NOW - 90_000 },
      ]),
    );
    inspect();
    const list = await screen.findByRole("list", { name: "Zones the device is in" });
    // The tab's own copy of a zone wins (it may have been renamed since); others show as sent.
    const names = within(list)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(names).toEqual(["Utrecht depot", "Harbour"]);
    expect(screen.queryByText("Outside all zones")).toBeNull();
    await userEvent.click(within(list).getByRole("button", { name: "Utrecht depot" }));
    expect(useUi.getState().selection).toEqual({ kind: "zone", id: "z-depot" });
  });

  it("says so when it is in none of the viewer's zones", async () => {
    api.getDevice.mockResolvedValue(device([]));
    inspect();
    expect(await screen.findByText("Outside all zones")).toBeTruthy();
  });
});

describe("device inspector for a streamed device", () => {
  it("lists every zone it is in, the most specific first", async () => {
    queryClient.setQueryData(ZONES_KEY, [
      zone({ id: "z-centre", name: "Centrum", radius_m: 2_000 }),
      zone({ id: "z-dam", name: "Dam Square", radius_m: 400 }),
      zone({ id: "z-paused", name: "Paused", radius_m: 900, is_active: false }),
    ]);
    (runtime.fleet as FleetStore).upsert("veh-7", 52.3733, 4.8929, NOW - 1_000, 3, 90);
    inspect();
    const list = await screen.findByRole("list", { name: "Zones the device is in" });
    expect(
      within(list)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Dam Square", "Centrum"]);
    expect(api.getDevice).not.toHaveBeenCalled();
  });
});
