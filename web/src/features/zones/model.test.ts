import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { ZonePatch } from "@/api/endpoints";
import { ApiError } from "@/api/http";
import type { Zone } from "@/api/schemas";
import {
  applyZoneEvent,
  bumpOccupancy,
  nextSwatch,
  nextZoneName,
  readZones,
  ZONE_SWATCHES,
  ZONES_KEY,
  ZonePatcher,
} from "./model";

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup(zones: Zone[] = [zone()]) {
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, zones);
  const calls: { id: string; patch: ZonePatch; version: number }[] = [];
  const replies: ReturnType<typeof deferred<Zone>>[] = [];
  const api = {
    update: vi.fn((id: string, patch: ZonePatch, version: number) => {
      calls.push({ id, patch, version });
      const reply = deferred<Zone>();
      replies.push(reply);
      return reply.promise;
    }),
    get: vi.fn(async (id: string) => zone({ id, radius_m: 999, version: 7 })),
  };
  const hooks = { onConflict: vi.fn(), onGone: vi.fn(), onError: vi.fn() };
  const patcher = new ZonePatcher(client, api, hooks);
  return { client, patcher, api, hooks, calls, replies };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("ZonePatcher", () => {
  it("applies edits immediately and sends them with the confirmed version", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    expect(readZones(t.client)[0]?.radius_m).toBe(500);
    expect(t.calls).toEqual([{ id: "z1", patch: { radius_m: 500 }, version: 1 }]);
    t.replies[0]?.resolve(zone({ radius_m: 500, version: 2 }));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 500, version: 2 });
  });

  it("serialises writes: edits made while one is in flight go out together, with the new version", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    t.patcher.patch("z1", { name: "Dam" });
    t.patcher.patch("z1", { radius_m: 650 });
    expect(t.calls).toHaveLength(1);
    expect(readZones(t.client)[0]).toMatchObject({ name: "Dam", radius_m: 650 });
    t.replies[0]?.resolve(zone({ radius_m: 500, version: 2 }));
    await flush();
    // The server's answer for the first write must not undo the edits still queued.
    expect(readZones(t.client)[0]).toMatchObject({ name: "Dam", radius_m: 650, version: 2 });
    expect(t.calls[1]).toEqual({ id: "z1", patch: { name: "Dam", radius_m: 650 }, version: 2 });
    t.replies[1]?.resolve(zone({ name: "Dam", radius_m: 650, version: 3 }));
    await t.patcher.settled("z1");
    expect(t.patcher.busy("z1")).toBe(false);
  });

  it("on 412 shows the latest server version and offers the local change back", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    t.replies[0]?.reject(new ApiError(412, { code: "precondition_failed" }, null));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 999, version: 7 });
    expect(t.hooks.onConflict).toHaveBeenCalledWith({
      zone: expect.objectContaining({ version: 7 }),
      change: { radius_m: 500 },
    });
  });

  it("drops a zone deleted elsewhere", async () => {
    const t = setup();
    t.patcher.patch("z1", { name: "x" });
    t.replies[0]?.reject(new ApiError(404, { code: "not_found" }, null));
    await t.patcher.settled("z1");
    expect(readZones(t.client)).toEqual([]);
    expect(t.hooks.onGone).toHaveBeenCalledWith("z1");
  });

  it("rolls back to the server state on other failures", async () => {
    const t = setup();
    t.patcher.patch("z1", { name: "x" });
    t.replies[0]?.reject(new Error("boom"));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]?.name).toBe("Dam Square");
    expect(t.hooks.onError).toHaveBeenCalled();
  });

  it("holds edits of a drawn zone until it exists, then saves them against the real id", async () => {
    const t = setup([zone({ id: "draft-1", version: 0 })]);
    t.patcher.patch("draft-1", { name: "Depot" });
    expect(t.calls).toHaveLength(0);
    t.client.setQueryData(ZONES_KEY, [zone({ id: "z9", version: 1 })]);
    t.patcher.rekey("draft-1", "z9");
    expect(t.calls).toEqual([{ id: "z9", patch: { name: "Depot" }, version: 1 }]);
  });
});

describe("zone events", () => {
  it("applies newer versions only and keeps unsaved local edits on top", () => {
    const client = new QueryClient();
    client.setQueryData(ZONES_KEY, [zone({ version: 3 })]);
    const none = () => undefined;
    expect(
      applyZoneEvent(
        client,
        { type: "zone.updated", zone: zone({ version: 3, radius_m: 1 }) },
        none,
      ),
    ).toBe(false);
    expect(readZones(client)[0]?.radius_m).toBe(400);
    const moved = applyZoneEvent(
      client,
      { type: "zone.updated", zone: zone({ version: 4, radius_m: 800, occupancy: undefined }) },
      none,
    );
    expect(moved).toBe(true);
    expect(readZones(client)[0]).toMatchObject({ radius_m: 800, occupancy: 3 });
    applyZoneEvent(
      client,
      { type: "zone.updated", zone: zone({ version: 5, radius_m: 900 }) },
      () => ({
        name: "Mine",
      }),
    );
    expect(readZones(client)[0]).toMatchObject({ radius_m: 900, name: "Mine" });
  });

  it("adds created zones and removes deleted ones", () => {
    const client = new QueryClient();
    client.setQueryData(ZONES_KEY, []);
    expect(
      applyZoneEvent(client, { type: "zone.created", zone: zone({ id: "n" }) }, () => undefined),
    ).toBe(true);
    expect(readZones(client).map((z) => z.id)).toEqual(["n"]);
    expect(applyZoneEvent(client, { type: "zone.deleted", id: "n" }, () => undefined)).toBe(true);
    expect(readZones(client)).toEqual([]);
  });

  it("moves occupancy with enter and exit alerts", () => {
    const client = new QueryClient();
    client.setQueryData(ZONES_KEY, [zone({ occupancy: 1 })]);
    bumpOccupancy(client, "z1", 1);
    bumpOccupancy(client, "z1", -5);
    expect(readZones(client)[0]?.occupancy).toBe(0);
  });
});

describe("before the first fetch", () => {
  it("never turns an event into a partial zone list that would pass for the real one", () => {
    const client = new QueryClient();
    applyZoneEvent(client, { type: "zone.created", zone: zone({ id: "early" }) }, () => undefined);
    bumpOccupancy(client, "z1", 1);
    expect(client.getQueryData(ZONES_KEY)).toBeUndefined();
  });
});

describe("new zone defaults", () => {
  it("picks the first unused swatch in validated order, then cycles", () => {
    expect(nextSwatch([])).toBe(ZONE_SWATCHES[0].color);
    expect(nextSwatch([zone({ color: "#6d5dfc" })])).toBe(ZONE_SWATCHES[1].color);
    const all = ZONE_SWATCHES.map((s, i) => zone({ id: `z${i}`, color: s.color }));
    expect(nextSwatch(all)).toBe(ZONE_SWATCHES[0].color);
  });

  it("numbers new zones without clashing", () => {
    expect(nextZoneName([])).toBe("Zone 1");
    expect(nextZoneName([zone({ name: "Zone 2" })])).toBe("Zone 3");
    expect(nextZoneName([zone({ name: "Zone 1" }), zone({ id: "b", name: "Zone 3" })])).toBe(
      "Zone 4",
    );
  });
});
