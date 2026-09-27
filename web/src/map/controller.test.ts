// @vitest-environment jsdom
import "@/test/dom";
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Zone } from "@/api/schemas";
import { FakeMap } from "@/test/maplibre";
import { MapController } from "./controller";

vi.mock("maplibre-gl", async () => (await import("@/test/maplibre")).fakeMapLibre);
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({ default: "worker.js" }));

async function mounted() {
  const container = document.createElement("div");
  document.body.append(container);
  const controller = new MapController();
  await controller.mount(container, "light");
  const map = FakeMap.created.at(-1);
  if (!map) throw new Error("no map was created");
  return { controller, map };
}

beforeEach(() => {
  // The basemap style comes from the tile host; unreachable here, the offline style stands in.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("offline");
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  FakeMap.created.length = 0;
  document.body.replaceChildren();
});

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
    occupancy: 3,
    ...overrides,
  };
}

describe("zone handles", () => {
  it("announce the radius as soon as they appear, and follow it", async () => {
    const { controller } = await mounted();
    controller.setZones([zone({ radius_m: 400 })]);
    controller.select({ kind: "zone", id: "z1" });
    const slider = screen.getByRole("slider", { name: "Zone radius" });
    expect(slider.getAttribute("aria-valuenow")).toBe("400");
    expect(slider.getAttribute("aria-valuetext")).toBe("400 m");
    // Resized in another session.
    controller.setZones([zone({ radius_m: 1_250, version: 2 })]);
    expect(slider.getAttribute("aria-valuenow")).toBe("1250");
    expect(slider.getAttribute("aria-valuetext")).toBe("1.25 km");
    controller.unmount();
  });
});

describe("the camera", () => {
  it("stops zooming out where the live channel's zoom range starts", async () => {
    const { controller, map } = await mounted();
    // A map shorter than a 512-pixel world could otherwise go below zoom 0, which no viewport
    // message may carry.
    expect(map.options.minZoom).toBe(0);
    controller.unmount();
  });
});
