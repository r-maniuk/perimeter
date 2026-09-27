// @vitest-environment jsdom
import "@/test/dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tooltip } from "radix-ui";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { ZoneInspector } from "./ZoneInspector";

const runtime = vi.hoisted(() => ({ patcher: { patch: vi.fn() } }));

vi.mock("@/app/runtime", () => ({ getRuntime: () => runtime }));
vi.mock("@/api/endpoints", () => ({
  zoneOccupants: vi.fn(async () => ({ occupancy: 0, items: [] })),
}));

function zone(overrides: Partial<Zone> = {}): Zone {
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

/** The inspector of a zone this big, with the keyboard on its radius slider. */
function radiusSlider(radius_m: number): HTMLElement {
  render(
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <ZoneInspector zone={zone({ radius_m })} onClose={() => {}} />
      </Tooltip.Provider>
    </QueryClientProvider>,
  );
  const slider = screen.getByRole("slider", { name: "Radius" });
  slider.focus();
  return slider;
}

afterEach(() => {
  cleanup();
  queryClient.clear();
  runtime.patcher.patch.mockReset();
});

describe("the radius slider from the keyboard", () => {
  it.each([
    [100, "{ArrowRight}", 110],
    [100, "{ArrowLeft}", 99],
    [1_000, "{ArrowUp}", 1_100],
    [2_000, "{ArrowDown}", 1_900],
    [437, "{ArrowRight}", 440],
    [250, "{PageUp}", 300],
    [250, "{Shift>}{ArrowLeft}{/Shift}", 200],
  ])("steps from %i m with %s to the next round radius, %i m", async (from, keys, to) => {
    radiusSlider(from);
    await userEvent.keyboard(keys);
    expect(runtime.patcher.patch).toHaveBeenCalledTimes(1);
    expect(runtime.patcher.patch).toHaveBeenCalledWith("z1", { radius_m: to });
  });

  it("saves once however long a key is held, where it was released", () => {
    const slider = radiusSlider(100);
    for (let press = 0; press < 5; press++) {
      fireEvent.keyDown(slider, { key: "ArrowRight", repeat: press > 0 });
    }
    expect(runtime.patcher.patch).not.toHaveBeenCalled();
    expect(slider.getAttribute("aria-valuetext")).toBe("150 m");
    fireEvent.keyUp(slider, { key: "ArrowRight" });
    expect(runtime.patcher.patch).toHaveBeenCalledTimes(1);
    expect(runtime.patcher.patch).toHaveBeenCalledWith("z1", { radius_m: 150 });
  });

  it("saves a step when the slider loses the keyboard before the key is released", () => {
    const slider = radiusSlider(100);
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    fireEvent.blur(slider);
    expect(runtime.patcher.patch).toHaveBeenCalledWith("z1", { radius_m: 110 });
  });

  it("sends nothing at either end of the scale", async () => {
    radiusSlider(100_000);
    await userEvent.keyboard("{ArrowRight}{PageUp}{End}");
    cleanup();
    radiusSlider(10);
    await userEvent.keyboard("{ArrowLeft}{PageDown}{Home}");
    expect(runtime.patcher.patch).not.toHaveBeenCalled();
  });
});
