import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { ZonePatch } from "@/api/endpoints";
import { ApiError, NetworkError } from "@/api/http";
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
  zonesContaining,
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

  it("undoes a failed edit even when the zone cannot be read back", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500, name: "Dam" });
    t.api.get.mockRejectedValueOnce(new NetworkError("network unavailable"));
    t.replies[0]?.reject(new NetworkError("network unavailable"));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 400, name: "Dam Square", version: 1 });
    expect(t.hooks.onError).toHaveBeenCalledTimes(1);
    expect(t.hooks.onConflict).not.toHaveBeenCalled();
  });

  it("falls back to the newest version the server announced while the write was on its way", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    // Another session renames the zone; its event arrives while this write is in flight.
    const renamed = zone({ name: "De Dam", version: 2 });
    applyZoneEvent(t.client, { type: "zone.updated", zone: renamed }, t.patcher);
    expect(readZones(t.client)[0]).toMatchObject({ name: "De Dam", radius_m: 500 });
    t.api.get.mockRejectedValueOnce(new NetworkError("network unavailable"));
    t.replies[0]?.reject(new ApiError(503, { code: "unavailable" }, 1));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]).toMatchObject({ name: "De Dam", radius_m: 400, version: 2 });
  });

  it("keeps the occupancy count when it undoes an edit", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    bumpOccupancy(t.client, "z1", 2);
    t.api.get.mockRejectedValueOnce(new NetworkError("network unavailable"));
    t.replies[0]?.reject(new NetworkError("network unavailable"));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 400, occupancy: 5 });
  });

  it("sends edits made while it recovers on top of the zone's latest version", async () => {
    const t = setup();
    const latest = deferred<Zone>();
    t.api.get.mockReturnValueOnce(latest.promise);
    t.patcher.patch("z1", { radius_m: 500 });
    t.replies[0]?.reject(new ApiError(412, { code: "precondition_failed" }, null));
    await flush();
    // Undone already, and still one write at a time: the new edit waits for the read.
    expect(readZones(t.client)[0]?.radius_m).toBe(400);
    t.patcher.patch("z1", { name: "Dam" });
    expect(t.calls).toHaveLength(1);
    latest.resolve(zone({ radius_m: 999, version: 7 }));
    await flush();
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 999, name: "Dam", version: 7 });
    expect(t.calls[1]).toEqual({ id: "z1", patch: { name: "Dam" }, version: 7 });
    expect(t.hooks.onConflict).toHaveBeenCalledWith({
      zone: expect.objectContaining({ version: 7 }),
      change: { radius_m: 500 },
    });
  });

  it("never lets its own answer undo a newer change announced meanwhile", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    const renamed = zone({ name: "De Dam", radius_m: 500, version: 3 });
    applyZoneEvent(t.client, { type: "zone.updated", zone: renamed }, t.patcher);
    t.replies[0]?.resolve(zone({ radius_m: 500, version: 2 }));
    await t.patcher.settled("z1");
    expect(readZones(t.client)[0]).toMatchObject({ name: "De Dam", radius_m: 500, version: 3 });
  });

  it("holds edits of a drawn zone until it exists, then saves them against the real id", async () => {
    const t = setup([zone({ id: "draft-1", version: 0 })]);
    t.patcher.patch("draft-1", { name: "Depot" });
    expect(t.calls).toHaveLength(0);
    t.client.setQueryData(ZONES_KEY, [zone({ id: "z9", version: 1 })]);
    t.patcher.rekey("draft-1", zone({ id: "z9", version: 1 }));
    expect(t.calls).toEqual([{ id: "z9", patch: { name: "Depot" }, version: 1 }]);
  });
});

describe("zone events", () => {
  it("applies newer versions only and keeps unsaved local edits on top", () => {
    const t = setup([zone({ version: 3 })]);
    const event = (overrides: Partial<Zone>) =>
      applyZoneEvent(t.client, { type: "zone.updated", zone: zone(overrides) }, t.patcher);
    expect(event({ version: 3, radius_m: 1 })).toBe(false);
    expect(readZones(t.client)[0]?.radius_m).toBe(400);
    expect(event({ version: 4, radius_m: 800, occupancy: undefined })).toBe(true);
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 800, occupancy: 3 });
    t.patcher.patch("z1", { name: "Mine" });
    event({ version: 5, radius_m: 900 });
    expect(readZones(t.client)[0]).toMatchObject({ radius_m: 900, name: "Mine" });
  });

  it("adds created zones and removes deleted ones", () => {
    const t = setup([]);
    const created = { type: "zone.created", zone: zone({ id: "n" }) } as const;
    expect(applyZoneEvent(t.client, created, t.patcher)).toBe(true);
    expect(readZones(t.client).map((z) => z.id)).toEqual(["n"]);
    expect(applyZoneEvent(t.client, { type: "zone.deleted", id: "n" }, t.patcher)).toBe(true);
    expect(readZones(t.client)).toEqual([]);
  });

  it("ignores a change published late, after a newer one", () => {
    const t = setup([]);
    const at = (version: number, radius_m: number) =>
      applyZoneEvent(
        t.client,
        { type: "zone.updated", zone: zone({ version, radius_m }) },
        t.patcher,
      );
    expect(at(5, 500)).toBe(true);
    expect(at(4, 400)).toBe(false);
    expect(at(5, 450)).toBe(false);
    expect(readZones(t.client)[0]).toMatchObject({ version: 5, radius_m: 500 });
  });

  it("never brings back a deleted zone, whatever about it is published late", () => {
    const t = setup([zone({ version: 2 })]);
    applyZoneEvent(t.client, { type: "zone.deleted", id: "z1" }, t.patcher);
    for (const type of ["zone.created", "zone.updated"] as const) {
      for (const version of [1, 2, 3]) {
        expect(applyZoneEvent(t.client, { type, zone: zone({ version }) }, t.patcher)).toBe(false);
      }
    }
    expect(readZones(t.client)).toEqual([]);
    // Nor does the zone's creation announced after its deletion.
    applyZoneEvent(t.client, { type: "zone.deleted", id: "brief" }, t.patcher);
    const created = { type: "zone.created", zone: zone({ id: "brief" }) } as const;
    expect(applyZoneEvent(t.client, created, t.patcher)).toBe(false);
    expect(readZones(t.client)).toEqual([]);
  });

  it("lets no answer to a write bring back a zone deleted while it was on its way", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    applyZoneEvent(t.client, { type: "zone.deleted", id: "z1" }, t.patcher);
    t.replies[0]?.resolve(zone({ radius_m: 500, version: 2 }));
    await t.patcher.settled("z1");
    expect(readZones(t.client)).toEqual([]);
  });

  it("reports a failed write to a zone deleted meanwhile as gone", async () => {
    const t = setup();
    t.patcher.patch("z1", { radius_m: 500 });
    applyZoneEvent(t.client, { type: "zone.deleted", id: "z1" }, t.patcher);
    t.replies[0]?.reject(new NetworkError("network unavailable"));
    await t.patcher.settled("z1");
    expect(readZones(t.client)).toEqual([]);
    expect(t.hooks.onGone).toHaveBeenCalledWith("z1");
    expect(t.hooks.onError).not.toHaveBeenCalled();
  });

  it("holds a zone this tab is deleting, and gives it back only if nobody else deleted it", () => {
    const t = setup([]);
    t.patcher.deleting("z1");
    expect(t.patcher.isNews(zone({ version: 9 }))).toBe(false);
    expect(t.patcher.undelete(zone({ version: 3 }))).toBe(true);
    expect(t.patcher.isNews(zone({ version: 3 }))).toBe(false);
    expect(t.patcher.isNews(zone({ version: 4 }))).toBe(true);
    t.patcher.deleting("z1");
    applyZoneEvent(t.client, { type: "zone.deleted", id: "z1" }, t.patcher);
    expect(t.patcher.undelete(zone({ version: 3 }))).toBe(false);
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
    const t = setup();
    const client = new QueryClient();
    applyZoneEvent(client, { type: "zone.created", zone: zone({ id: "early" }) }, t.patcher);
    bumpOccupancy(client, "z1", 1);
    expect(client.getQueryData(ZONES_KEY)).toBeUndefined();
  });
});

describe("zones around a point", () => {
  it("lists the active zones containing it, the smallest first", () => {
    const zones = [
      zone({ id: "city", radius_m: 5_000 }),
      zone({ id: "square", radius_m: 150 }),
      zone({ id: "paused", radius_m: 300, is_active: false }),
      zone({ id: "elsewhere", center: { lat: 52.1, lon: 5.1 }, radius_m: 900 }),
    ];
    expect(zonesContaining(zones, 52.3735, 4.8929).map((z) => z.id)).toEqual(["square", "city"]);
    // 500 m from the centre: outside the square, inside the city.
    expect(zonesContaining(zones, 52.3776, 4.8926).map((z) => z.id)).toEqual(["city"]);
    expect(zonesContaining(zones, 51, 3)).toEqual([]);
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
