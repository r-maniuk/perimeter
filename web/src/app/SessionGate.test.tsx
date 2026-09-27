// @vitest-environment jsdom
import "@/test/dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tooltip } from "radix-ui";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { User, Zone } from "@/api/schemas";
import { useNotices } from "@/features/shell/notices";
import { readZones } from "@/features/zones/model";
import { useSession } from "@/state/session";
import { useUi } from "@/state/ui";
import { queryClient } from "./queryClient";
import { SessionGate } from "./SessionGate";

const GRACE: User = { id: "u-grace", username: "grace" };
const ADA: User = { id: "u-ada", username: "ada" };

/** The session cookie every tab of the browser sends: whom the server answers for. */
const browser = vi.hoisted(() => ({ cookie: null as { id: string; username: string } | null }));

const api = vi.hoisted(() => ({
  currentUser: vi.fn(async () => browser.cookie),
  signIn: vi.fn(),
  listZones: vi.fn(),
}));
const runtime = vi.hoisted(() => ({ startRuntime: vi.fn(), stopRuntime: vi.fn() }));

vi.mock("@/api/endpoints", () => api);
vi.mock("./runtime", () => ({ ...runtime, getRuntime: () => null }));
// The map is not what this is about (and needs WebGL).
vi.mock("@/map/MapStage", () => ({ MapStage: () => null }));

function zone(id: string, name: string): Zone {
  return {
    id,
    name,
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
  };
}

/** Each account's zones. */
const zonesOf: Record<string, Zone[]> = {
  [GRACE.id]: [zone("z-grace", "Harbour")],
  [ADA.id]: [zone("z-ada", "Depot")],
};

/** A tab of the browser, opened while the cookie belongs to `cookie`. */
function openTab(cookie: User | null) {
  browser.cookie = cookie;
  api.currentUser.mockImplementation(async () => browser.cookie);
  api.listZones.mockImplementation(async () => zonesOf[browser.cookie?.id ?? ""] ?? []);
  api.signIn.mockImplementation(async (username: string) => {
    browser.cookie = [GRACE, ADA].find((u) => u.username === username) ?? null;
    return { expires_at: "2026-09-28T10:00:00Z", user: browser.cookie };
  });
  render(
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <SessionGate />
      </Tooltip.Provider>
    </QueryClientProvider>,
  );
}

const zoneNames = () => readZones(queryClient).map((z) => z.name);

beforeAll(async () => {
  // The workspace is its own chunk, loaded on first sign-in: have it ready.
  await import("@/features/shell/Shell");
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  useSession.setState({ status: "checking", user: null, notice: null });
  useUi.setState({ panel: null, selection: null, sheet: "peek" });
  useNotices.setState({ notices: [] });
  for (const mock of [...Object.values(api), ...Object.values(runtime)]) mock.mockReset();
});

describe("switching accounts in place", () => {
  it("loads the next account's zones at once, not at the next periodic refresh", async () => {
    openTab(GRACE);
    await waitFor(() => expect(zoneNames()).toEqual(["Harbour"]));
    expect(runtime.startRuntime).toHaveBeenLastCalledWith(GRACE, queryClient);

    // Another tab of this browser signed in as ada. The map draws what the cache holds.
    browser.cookie = ADA;
    act(() => useSession.getState().signedIn(ADA));
    await waitFor(() => expect(zoneNames()).toEqual(["Depot"]));
    expect(runtime.stopRuntime).toHaveBeenCalledTimes(1);
    expect(runtime.startRuntime).toHaveBeenLastCalledWith(ADA, queryClient);
  });

  it("carries nothing of the last account's workspace over to the next", async () => {
    openTab(GRACE);
    await waitFor(() => expect(readZones(queryClient)).toHaveLength(1));
    act(() => useUi.getState().openPanel("zones"));
    act(() => useUi.getState().select({ kind: "zone", id: "z-grace" }));
    browser.cookie = ADA;
    act(() => useSession.getState().signedIn(ADA));
    await waitFor(() => expect(runtime.startRuntime).toHaveBeenLastCalledWith(ADA, queryClient));
    expect(useUi.getState()).toMatchObject({ panel: null, selection: null, sheet: "peek" });
  });
});

describe("tabs of one browser", () => {
  it("follow each other's sign-ins, without waiting for their sockets to reconnect", async () => {
    const otherTab = new BroadcastChannel("perimeter.session");
    const heard: unknown[] = [];
    otherTab.addEventListener("message", (event) => heard.push(event.data));
    try {
      // Signed out in every tab; this one signs in as grace.
      openTab(null);
      await userEvent.type(await screen.findByLabelText("Username"), "grace{Enter}");
      await waitFor(() => expect(zoneNames()).toEqual(["Harbour"]));
      await vi.waitFor(() => expect(heard).toEqual([{ change: "signedIn" }]));

      // Then the other tab signs in as ada: from now on every request of this tab is ada's.
      browser.cookie = ADA;
      otherTab.postMessage({ change: "signedIn" });
      await waitFor(() => expect(useSession.getState().user).toEqual(ADA));
      await waitFor(() => expect(zoneNames()).toEqual(["Depot"]));
      expect(runtime.startRuntime).toHaveBeenLastCalledWith(ADA, queryClient);
      expect(useNotices.getState().notices).toEqual([
        expect.objectContaining({ title: "Signed in as ada" }),
      ]);

      // And signs out: this tab goes to the sign-in card with it.
      browser.cookie = null;
      otherTab.postMessage({ change: "signedOut" });
      await waitFor(() => expect(useSession.getState().status).toBe("signedOut"));
      expect(await screen.findByText("You were signed out. Sign in again.")).toBeTruthy();
      expect(runtime.stopRuntime).toHaveBeenCalledTimes(2);
    } finally {
      otherTab.close();
    }
  });
});

describe("the sign-in card", () => {
  it("is ready again when a sign-out comes while the last card is still leaving", async () => {
    // The card leaves with an animation, which waits while its tab is hidden: a sign-out made in
    // another tab meanwhile brings the card back before it ever left.
    const off = useSession.subscribe((now, before) => {
      if (now.status === "signedIn" && before.status !== "signedIn") {
        off();
        useSession.getState().signedOut("You were signed out. Sign in again.");
      }
    });
    openTab(null);
    const username = await screen.findByLabelText("Username");
    await userEvent.clear(username);
    await userEvent.type(username, "grace{Enter}");
    await waitFor(() => expect(api.signIn).toHaveBeenCalledWith("grace"));
    await waitFor(() => expect(useSession.getState().status).toBe("signedOut"));
    const button = await screen.findByRole("button", { name: /Continue/ });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    // The card that was signing in finishes leaving; only the fresh one stays.
    await waitFor(() => expect(screen.queryByText("Signing in")).toBeNull(), { timeout: 3_000 });
  });
});
