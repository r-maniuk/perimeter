// @vitest-environment jsdom
import "@/test/dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tooltip } from "radix-ui";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/app/queryClient";
import { CommandPalette } from "@/features/command/CommandPalette";
import { Rail } from "@/features/shell/Rail";
import { ZONES_KEY } from "@/features/zones/model";
import { ZonesPanel } from "@/features/zones/ZonesPanel";
import { usePointer } from "@/state/pointer";
import { useUi } from "@/state/ui";
import { FakeMap } from "@/test/maplibre";
import { HOME } from "./controller";
import { MapStage } from "./MapStage";

vi.mock("maplibre-gl", async () => (await import("@/test/maplibre")).fakeMapLibre);
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({ default: "worker.js" }));

const actions = vi.hoisted(() => ({ createDrawnZone: vi.fn(), commitZoneEdit: vi.fn() }));

vi.mock("@/features/zones/actions", () => actions);
vi.mock("@/api/endpoints", () => ({ listZones: vi.fn(async () => []) }));
vi.mock("@/app/runtime", () => ({ getRuntime: () => null }));

/** The map, and `entry` — the part of the workspace drawing is started from. */
async function workspace(entry: ReactNode) {
  render(
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <MapStage signedIn />
        {entry}
      </Tooltip.Provider>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(usePointer.getState().mapReady).toBe(true));
}

/** Enter, as the draw hint offers it: a zone at the centre of the map. */
async function pressEnterForAZoneAtTheCentre() {
  await userEvent.keyboard("{Enter}");
  expect(useUi.getState().drawing).toBe(true);
  expect(actions.createDrawnZone).toHaveBeenCalledTimes(1);
  const [shape] = actions.createDrawnZone.mock.calls[0] as [{ lat: number; lon: number }];
  expect(shape.lat).toBeCloseTo(HOME.lat, 6);
  expect(shape.lon).toBeCloseTo(HOME.lon, 6);
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("offline");
    }),
  );
  queryClient.setQueryData(ZONES_KEY, []);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  queryClient.clear();
  useUi.setState({ drawing: false, commandOpen: false, panel: null });
  FakeMap.created.length = 0;
});

describe("starting to draw", () => {
  it("from the rail leaves Enter to the map, not to the button that started it", async () => {
    await workspace(<Rail />);
    await userEvent.click(screen.getByRole("button", { name: "Draw a zone" }));
    expect(document.activeElement).toBe(FakeMap.created.at(-1)?.canvas);
    await pressEnterForAZoneAtTheCentre();
  });

  it("from the Zones panel leaves Enter to the map", async () => {
    await workspace(<ZonesPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Draw" }));
    await pressEnterForAZoneAtTheCentre();
  });

  it("from the command palette leaves Enter to the map once the palette has closed", async () => {
    await workspace(
      <>
        <button type="button" onClick={() => useUi.getState().setCommandOpen(true)}>
          Search
        </button>
        <CommandPalette />
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await userEvent.click(await screen.findByRole("option", { name: /Draw a zone/ }));
    // A closing dialog settles focus a moment later: the map must still have it then.
    await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(document.activeElement).toBe(FakeMap.created.at(-1)?.canvas);
    await pressEnterForAZoneAtTheCentre();
  });
});
