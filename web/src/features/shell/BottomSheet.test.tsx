// @vitest-environment jsdom
import "@/test/dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tooltip } from "radix-ui";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { ZONES_KEY } from "@/features/zones/model";
import { mapController } from "@/map/controller";
import { useUi } from "@/state/ui";
import { BottomSheet } from "./BottomSheet";

vi.mock("@/api/endpoints", () => ({
  zoneOccupants: vi.fn(async () => ({ occupancy: 0, items: [] })),
  listZones: vi.fn(),
}));
vi.mock("@/app/runtime", () => ({ getRuntime: () => null }));

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
    occupancy: 0,
    ...overrides,
  };
}

beforeEach(() => {
  queryClient.setQueryData(ZONES_KEY, [
    zone({ id: "z-dock", name: "Dock", dwell_s: 600 }),
    zone({ id: "z-gate", name: "Gate", dwell_s: null }),
  ]);
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  useUi.setState({ panel: null, selection: null, sheet: "peek" });
});

function sheet() {
  return render(
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <BottomSheet />
      </Tooltip.Provider>
    </QueryClientProvider>,
  );
}

/** How high the sheet stands: the map frames what it shows above that. */
function standing(insets: { mock: { calls: unknown[][] } }) {
  const [last] = insets.mock.calls.at(-1) ?? [];
  return (last as { bottom?: number } | undefined)?.bottom;
}

function dwellChoice(name: string) {
  const group = screen.getByRole("radiogroup", { name: "Dwell alert" });
  return within(group).getByRole("radio", { name }).getAttribute("aria-checked");
}

describe("phone sheet", () => {
  it("starts every zone's inspector afresh instead of carrying the last one's state", async () => {
    act(() => useUi.getState().select({ kind: "zone", id: "z-dock" }));
    render(
      <QueryClientProvider client={queryClient}>
        <Tooltip.Provider>
          <BottomSheet />
        </Tooltip.Provider>
      </QueryClientProvider>,
    );
    // The dock alerts after a custom ten minutes; its field is open with that value.
    expect(dwellChoice("Custom")).toBe("true");
    expect(screen.getByLabelText("Alert after")).toHaveProperty("value", "10");
    // A name half typed on the dock stays with the dock.
    await userEvent.type(screen.getByRole("textbox", { name: "Zone name" }), " North");

    act(() => useUi.getState().select({ kind: "zone", id: "z-gate" }));
    expect(dwellChoice("Off")).toBe("true");
    expect(dwellChoice("Custom")).toBe("false");
    expect(screen.queryByLabelText("Alert after")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Zone name" })).toHaveProperty("value", "Gate");
  });

  it("rests at peek once what it showed was closed on a desktop layout", () => {
    // Opened while the window was wide: the sheet opens with it, for when the window narrows.
    act(() => useUi.getState().openPanel("zones"));
    expect(useUi.getState().sheet).toBe("half");
    // Closed there, then the window narrows to a phone.
    act(() => useUi.getState().openPanel(null));
    expect(useUi.getState().sheet).toBe("peek");
    const insets = vi.spyOn(mapController, "setInsets");
    sheet();
    expect(standing(insets)).toBe(92);
    expect(screen.getByRole("button", { name: "Expand" })).toHaveProperty("disabled", true);
    const tabs = within(screen.getByRole("navigation", { name: "Workspace" }));
    expect(tabs.getAllByRole("button").map((tab) => tab.getAttribute("aria-pressed"))).toEqual(
      Array(5).fill("false"),
    );
  });

  it("goes back to peek with the last thing it shows, wherever that closes", () => {
    const ui = useUi.getState;
    act(() => ui().openPanel("zones"));
    act(() => ui().select({ kind: "zone", id: "z-dock" }));
    act(() => ui().select(null));
    // The panel is still there to show.
    expect(ui().sheet).toBe("half");
    act(() => ui().togglePanel("zones"));
    expect(ui().sheet).toBe("peek");
    act(() => ui().select({ kind: "device", id: "veh-1" }));
    expect(ui().sheet).toBe("half");
    // Cleared by the app itself (the zone was deleted in another session, say).
    act(() => ui().select(null));
    expect(ui().sheet).toBe("peek");
  });

  it("rests at peek while the zone it was opened for is gone from the list", () => {
    useUi.setState({ panel: null, selection: { kind: "zone", id: "z-gone" }, sheet: "half" });
    const insets = vi.spyOn(mapController, "setInsets");
    sheet();
    expect(standing(insets)).toBe(92);
    expect(screen.getByRole("button", { name: "Expand" })).toHaveProperty("disabled", true);
    // Something to show opens it again.
    act(() => useUi.getState().openPanel("zones"));
    expect(standing(insets)).toBeGreaterThan(92);
    expect(screen.getByRole("button", { name: "Collapse" })).toHaveProperty("disabled", false);
  });
});
