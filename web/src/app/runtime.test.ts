import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EventEnvelope, HelloFrame, User, Zone } from "@/api/schemas";
import { useNotices } from "@/features/shell/notices";
import { readZones, ZONES_KEY } from "@/features/zones/model";
import { FrameKind } from "@/lib/frames";
import { tileFor } from "@/lib/tiles";
import type { LiveEvent, Viewport } from "@/live/client";
import { mapController } from "@/map/controller";
import { useAlerts } from "@/state/alerts";
import { useLive } from "@/state/live";
import { useSession } from "@/state/session";
import { encodeBundle, encodeTile } from "@/test/frames";
import { Runtime } from "./runtime";

/** The live client as the runtime sees it: events are pushed in by the test. */
const live = vi.hoisted(() => ({
  instances: [] as {
    emit(event: LiveEvent): void;
    setViewport: ReturnType<typeof vi.fn>;
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
    lastSeq: number | null;
    clock: { now(): number; offsetMs: number };
  }[],
}));

vi.mock("@/live/client", () => ({
  liveUrl: () => "ws://test/v1/live",
  sessionSeqStore: () => ({ get: () => null, set: () => {} }),
  LiveClient: class {
    listeners = new Set<(event: LiveEvent) => void>();
    setViewport = vi.fn();
    setOps = vi.fn();
    start = vi.fn();
    stop = vi.fn();
    retryNow = vi.fn();
    lastSeq: number | null = null;
    clock = { now: () => Date.now(), offsetMs: 0 };
    constructor() {
      live.instances.push(this);
    }
    subscribe(listener: (event: LiveEvent) => void) {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    }
    emit(event: LiveEvent) {
      for (const listener of this.listeners) listener(event);
    }
  },
}));

const endpoints = vi.hoisted(() => ({ currentUser: vi.fn() }));

vi.mock("@/api/endpoints", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/endpoints")>()),
  currentUser: endpoints.currentUser,
}));

const map = vi.hoisted(() => ({
  viewport: null as Viewport | null,
}));

vi.mock("@/map/controller", () => ({
  mapController: {
    onViewport: null,
    attachFleet: vi.fn(),
    detachFleet: vi.fn(),
    poke: vi.fn(),
    onPositions: vi.fn(),
    setZones: vi.fn(),
    pulse: vi.fn(),
    viewport: () => map.viewport,
  },
}));

const USER: User = { id: "u-ada", username: "ada" };
const NOW = Date.parse("2026-09-27T10:00:00Z");
/** Central Amsterdam at street level: a handful of zoom-12 tiles. */
const VIEW: Viewport = { bbox: [4.88, 52.365, 4.9, 52.375], zoom: 15 };

function hello(overrides: Partial<HelloFrame> = {}): HelloFrame {
  return {
    type: "hello",
    session_id: "s-1",
    user: { id: USER.id, username: USER.username },
    server_time: NOW,
    protocol: 1,
    resume: { mode: "fresh", after: 0 },
    tile_zoom: 12,
    replica: "api-1",
    ...overrides,
  };
}

function setup() {
  const client = new QueryClient();
  const runtime = new Runtime(USER, client);
  runtime.start();
  const channel = live.instances.at(-1);
  if (!channel) throw new Error("no live client");
  const emit = (event: LiveEvent) => channel.emit(event);
  return { client, runtime, channel, emit };
}

function zoneDeleted(id: string): EventEnvelope {
  return { id: `evt-${id}`, type: "zone.deleted", ts: new Date(NOW).toISOString(), data: { id } };
}

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

/** An alert event as the engine publishes it: the event id is the alert's own id. */
function alert(id: string, kind: "enter" | "exit" | "dwell" = "enter", ts = NOW): EventEnvelope {
  const at = new Date(ts).toISOString();
  return {
    id,
    type: "alert",
    ts: at,
    data: {
      alert_id: id,
      kind,
      device_id: "veh-1",
      zone: { id: "z1", name: "Dam Square" },
      occurred_at: at,
      position: { lat: 52.3731, lon: 4.8926 },
    },
  };
}

/** One position of `deviceId` at (`lat`, `lon`), in the tile frame the server would send. */
function positionAt(deviceId: string, lat: number, lon: number): ArrayBuffer {
  const tile = tileFor(lon, lat, 12);
  return encodeBundle([
    encodeTile(FrameKind.Live, 12, tile.x, tile.y, [
      { deviceId, lat, lon, recordedAtMs: NOW, speedMps: 5, headingDeg: 90 },
    ]),
  ]);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  map.viewport = VIEW;
  useSession.getState().signedIn(USER);
  vi.mocked(mapController.setZones).mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  live.instances.length = 0;
  useLive.getState().reset();
  useAlerts.getState().reset();
  useNotices.setState({ notices: [] });
  endpoints.currentUser.mockReset();
});

describe("identity", () => {
  it("follows another account signed in elsewhere in this browser", async () => {
    const grace = { id: "u-grace", username: "grace" };
    endpoints.currentUser.mockResolvedValue(grace);
    const t = setup();
    t.emit({ type: "accountChanged", user: grace });
    await vi.waitFor(() => expect(useSession.getState().user).toEqual(grace));
    expect(useNotices.getState().notices).toEqual([
      expect.objectContaining({ tone: "info", title: "Signed in as grace" }),
    ]);
  });

  it("signs out when nobody is signed in any more", async () => {
    endpoints.currentUser.mockResolvedValue(null);
    const t = setup();
    t.emit({ type: "accountChanged", user: { id: "u-grace", username: "grace" } });
    await vi.waitFor(() => expect(useSession.getState().status).toBe("signedOut"));
    expect(useSession.getState().notice).toMatch(/session has ended/);
  });

  it("stays put when the browser is back on this account, or cannot tell yet", async () => {
    const t = setup();
    endpoints.currentUser.mockResolvedValueOnce(USER);
    t.emit({ type: "accountChanged", user: { id: "u-grace", username: "grace" } });
    endpoints.currentUser.mockRejectedValueOnce(new Error("offline"));
    t.emit({ type: "accountChanged", user: { id: "u-grace", username: "grace" } });
    await vi.waitFor(() => expect(endpoints.currentUser).toHaveBeenCalledTimes(2));
    await Promise.resolve();
    expect(useSession.getState()).toMatchObject({ status: "signedIn", user: USER });
    expect(useNotices.getState().notices).toEqual([]);
  });
});

describe("connection facts", () => {
  it("publishes the last event and the clock offset as they change", () => {
    const t = setup();
    t.channel.clock.offsetMs = 250;
    t.emit({ type: "hello", hello: hello({ resume: { mode: "replay", after: 40 } }) });
    expect(useLive.getState()).toMatchObject({ lastSeq: 40, clockOffsetMs: 250, replica: "api-1" });
    t.emit({ type: "event", seq: 41, event: zoneDeleted("z-gone"), replayed: false });
    expect(useLive.getState().lastSeq).toBe(41);
    t.channel.clock.offsetMs = -80;
    t.emit({ type: "latency", rttMs: 14 });
    expect(useLive.getState()).toMatchObject({ latencyMs: 14, clockOffsetMs: -80 });
  });
});

describe("alerts", () => {
  it("applies an alert delivered twice under different sequence numbers only once", () => {
    const t = setup();
    t.client.setQueryData(ZONES_KEY, [zone({ occupancy: 3 })]);
    t.emit({ type: "hello", hello: hello() });
    t.emit({ type: "event", seq: 7, event: alert("a-1"), replayed: false });
    // The outbox published it again after the broker had forgotten the first copy.
    t.emit({ type: "event", seq: 12, event: alert("a-1"), replayed: false });
    expect(useAlerts.getState().live.map((a) => a.id)).toEqual(["a-1"]);
    expect(useAlerts.getState().toasts).toHaveLength(1);
    expect(useAlerts.getState().toasts[0]?.count).toBe(1);
    expect(useAlerts.getState().unseen).toBe(1);
    expect(readZones(t.client)[0]?.occupancy).toBe(4);
    // The stream position still moves past the copy.
    expect(useLive.getState().lastSeq).toBe(12);
  });

  it("counts arrivals once after a reload: replayed ones are in the re-read counts", () => {
    const t = setup();
    // The list fetched on load already counts the device that entered while the page reloaded.
    t.client.setQueryData(ZONES_KEY, [zone({ occupancy: 4 })]);
    t.emit({ type: "hello", hello: hello({ resume: { mode: "replay", after: 6 } }) });
    expect(t.client.getQueryState(ZONES_KEY)?.isInvalidated).toBe(true);
    t.emit({ type: "event", seq: 7, event: alert("a-away", "enter", NOW - 5_000), replayed: true });
    t.emit({ type: "event", seq: 8, event: alert("a-exit", "exit", NOW - 4_000), replayed: true });
    expect(readZones(t.client)[0]?.occupancy).toBe(4);
    // The replayed alerts still reach the timeline, without toasts.
    expect(useAlerts.getState().live.map((a) => a.id)).toEqual(["a-exit", "a-away"]);
    expect(useAlerts.getState().toasts).toEqual([]);
    expect(useLive.getState().away?.count).toBe(2);
    // Live ones move the count as before.
    t.emit({ type: "event", seq: 9, event: alert("a-now", "enter"), replayed: false });
    expect(readZones(t.client)[0]?.occupancy).toBe(5);
  });

  it("re-reads nothing after a fresh start", () => {
    const t = setup();
    t.client.setQueryData(ZONES_KEY, [zone()]);
    t.emit({ type: "hello", hello: hello() });
    expect(t.client.getQueryState(ZONES_KEY)?.isInvalidated).toBe(false);
  });

  it("recognises a copy far behind the alerts the timeline keeps", () => {
    const t = setup();
    t.client.setQueryData(ZONES_KEY, [zone({ occupancy: 0 })]);
    t.emit({ type: "hello", hello: hello() });
    t.emit({ type: "event", seq: 1, event: alert("a-first"), replayed: false });
    for (let k = 0; k < 600; k++) {
      t.emit({ type: "event", seq: 2 + k, event: alert(`a-${k}`, "dwell"), replayed: false });
    }
    t.emit({ type: "event", seq: 700, event: alert("a-first"), replayed: false });
    expect(readZones(t.client)[0]?.occupancy).toBe(1);
  });
});

describe("zones on the map", () => {
  function zoneEvent(type: "zone.created" | "zone.updated", z: Zone): EventEnvelope {
    return { id: `evt-${z.id}-${z.version}`, type, ts: new Date(NOW).toISOString(), data: z };
  }

  it("leaves the zone layers alone when only occupancy counts move", () => {
    const t = setup();
    t.client.setQueryData(ZONES_KEY, [zone({ occupancy: 3 })]);
    t.emit({ type: "hello", hello: hello() });
    vi.mocked(mapController.setZones).mockClear();
    for (let k = 0; k < 40; k++) {
      t.emit({
        type: "event",
        seq: k + 1,
        event: alert(`a-${k}`, k % 2 ? "exit" : "enter"),
        replayed: false,
      });
    }
    expect(mapController.setZones).not.toHaveBeenCalled();
    // A change the map does draw still goes through.
    t.client.setQueryData(ZONES_KEY, [zone({ occupancy: 3, color: "#e05555" })]);
    expect(mapController.setZones).toHaveBeenCalledTimes(1);
  });

  it("animates a zone another session moved, from where the map showed it", () => {
    const t = setup();
    t.client.setQueryData(ZONES_KEY, [zone({ version: 1 })]);
    vi.mocked(mapController.setZones).mockClear();
    const moved = zone({ version: 2, center: { lat: 52.38, lon: 4.9 } });
    t.emit({ type: "event", seq: 1, event: zoneEvent("zone.updated", moved), replayed: false });
    expect(mapController.setZones).toHaveBeenCalledTimes(1);
    expect(mapController.setZones).toHaveBeenLastCalledWith(
      [expect.objectContaining({ center: { lat: 52.38, lon: 4.9 } })],
      new Set(["z1"]),
    );
    t.emit({ type: "event", seq: 2, event: zoneDeleted("z1"), replayed: false });
    expect(mapController.setZones).toHaveBeenLastCalledWith([], new Set(["z1"]));
  });

  it("takes its zones off the map when the session ends, so the next account never sees them", () => {
    const first = setup();
    first.client.setQueryData(ZONES_KEY, [zone()]);
    expect(mapController.setZones).toHaveBeenLastCalledWith([zone()], new Set());
    first.runtime.stop();
    expect(mapController.setZones).toHaveBeenLastCalledWith([]);
    // The next account has no zones: its first sync still reaches the map.
    vi.mocked(mapController.setZones).mockClear();
    const next = setup();
    next.client.setQueryData(ZONES_KEY, []);
    expect(mapController.setZones).toHaveBeenCalledWith([], new Set());
  });

  it("reads a zone's occupants at once, then at most once a second while alerts keep coming", () => {
    const t = setup();
    t.client.setQueryData(ZONES_KEY, [zone({ occupancy: 0 })]);
    const invalidate = vi.spyOn(t.client, "invalidateQueries");
    const reads = () =>
      invalidate.mock.calls.filter(([filters]) => filters?.queryKey?.[0] === "occupants").length;
    // Twenty arrivals within 900 ms.
    for (let k = 0; k < 20; k++) {
      t.emit({ type: "event", seq: k + 1, event: alert(`a-${k}`), replayed: false });
      vi.advanceTimersByTime(45);
    }
    expect(reads()).toBe(1);
    vi.advanceTimersByTime(100);
    expect(reads()).toBe(2);
    vi.advanceTimersByTime(5_000);
    expect(reads()).toBe(2);
    expect(invalidate).toHaveBeenCalledWith(
      { queryKey: ["occupants", "z1"] },
      { cancelRefetch: false },
    );
  });
});

describe("viewport and coverage", () => {
  it("trims the fleet when the cover changes, not on every camera report", () => {
    const t = setup();
    t.emit({ type: "hello", hello: hello() });
    const retain = vi.spyOn(t.runtime.fleet, "retainCoverage");
    // A camera following a device reports a slightly different view many times a second.
    for (let frame = 0; frame < 120; frame++) {
      const shift = frame * 1e-6;
      t.runtime.setViewport({ bbox: [4.88 + shift, 52.365, 4.9 + shift, 52.375], zoom: 15 });
    }
    expect(retain).not.toHaveBeenCalled();
    expect(t.channel.setViewport).toHaveBeenCalledTimes(121);
    t.runtime.setViewport({ bbox: [5.5, 52.0, 5.52, 52.01], zoom: 15 });
    expect(retain).toHaveBeenCalledTimes(1);
  });

  it("ignores positions from tiles the view has already left", () => {
    const t = setup();
    t.emit({ type: "hello", hello: hello() });
    t.emit({ type: "positions", data: positionAt("inside", 52.37, 4.89) });
    t.emit({ type: "positions", data: positionAt("left-behind", 52.0, 5.6) });
    expect(t.runtime.fleet.has("inside")).toBe(true);
    expect(t.runtime.fleet.has("left-behind")).toBe(false);
  });
});
