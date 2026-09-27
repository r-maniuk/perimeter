import { afterEach, describe, expect, it, vi } from "vitest";
import {
  alertFromRecord,
  deviceTrail,
  listAlerts,
  listSessions,
  listZones,
  revokeSession,
  updateZone,
  zoneOccupants,
} from "./endpoints";
import { ApiError, ContractError } from "./http";

function respond(body: unknown, init: ResponseInit = {}) {
  const fetchMock = vi.fn(async () => Response.json(body, init));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

const zone = {
  id: "z1",
  name: "Dam Square",
  color: "#6d5dfc",
  center: { lat: 52.3731, lon: 4.8926 },
  radius_m: 250,
  is_active: true,
  notify_enter: true,
  notify_exit: true,
  dwell_s: 300,
  version: 3,
  created_at: "2026-09-26T19:00:00Z",
  updated_at: "2026-09-26T19:07:06Z",
  occupancy: 12,
};

describe("REST contract", () => {
  it("follows keyset pages until the cursor runs out", async () => {
    const pages = [
      { items: [zone], next_cursor: "abc" },
      { items: [{ ...zone, id: "z2" }], next_cursor: null },
    ];
    const fetchMock = vi.fn(async () => Response.json(pages.shift()));
    vi.stubGlobal("fetch", fetchMock);
    const zones = await listZones();
    expect(zones.map((z) => z.id)).toEqual(["z1", "z2"]);
    expect(String((fetchMock.mock.calls[1] as unknown[])[0])).toContain("cursor=abc");
  });

  it("reads alerts with their nested zone, which may have been deleted", async () => {
    respond({
      items: [
        {
          id: "a1",
          kind: "enter",
          device_id: "veh-00042",
          zone: { id: null, name: "Old depot" },
          position: { lat: 52.3729, lon: 4.8931 },
          occurred_at: "2026-09-26T19:07:06.131Z",
          created_at: "2026-09-26T19:07:06.162Z",
        },
      ],
      next_cursor: null,
    });
    const page = await listAlerts({ kind: "enter", limit: 10 });
    expect(page.items[0]).toEqual({
      id: "a1",
      kind: "enter",
      deviceId: "veh-00042",
      zoneId: null,
      zoneName: "Old depot",
      lat: 52.3729,
      lon: 4.8931,
      occurredAt: Date.parse("2026-09-26T19:07:06.131Z"),
    });
    expect(
      alertFromRecord({
        id: "x",
        kind: "dwell",
        device_id: "d",
        zone: { id: "z1", name: "Dam" },
        position: { lat: 1, lon: 2 },
        occurred_at: "2026-01-01T00:00:00Z",
      }).zoneId,
    ).toBe("z1");
  });

  it("reads trails as lines, single points or nothing", async () => {
    respond({
      type: "Feature",
      id: "veh-1",
      geometry: {
        type: "LineString",
        coordinates: [
          [4.1, 52.1],
          [4.2, 52.2],
        ],
      },
      properties: {
        device_id: "veh-1",
        since: "2026-09-26T19:00:00Z",
        timestamps: ["2026-09-26T19:00:01Z", "2026-09-26T19:00:04Z"],
        speeds: [3, null],
        complete: false,
      },
    });
    const line = await deviceTrail("veh-1", 15);
    expect(line.coordinates).toEqual([
      [4.1, 52.1],
      [4.2, 52.2],
    ]);
    expect(line.times).toEqual([
      Date.parse("2026-09-26T19:00:01Z"),
      Date.parse("2026-09-26T19:00:04Z"),
    ]);
    expect(line.complete).toBe(false);

    respond({
      type: "Feature",
      id: "p",
      geometry: { type: "Point", coordinates: [4.3, 52.3] },
      properties: {
        device_id: "p",
        since: "2026-09-26T19:00:00Z",
        timestamps: ["2026-09-26T19:00:01Z"],
        speeds: [null],
        complete: true,
      },
    });
    expect((await deviceTrail("p", 5)).coordinates).toEqual([[4.3, 52.3]]);

    respond({
      type: "Feature",
      id: "n",
      geometry: null,
      properties: {
        device_id: "n",
        since: "2026-09-26T19:00:00Z",
        timestamps: [],
        speeds: [],
        complete: true,
      },
    });
    expect(await deviceTrail("n", 5)).toEqual({
      coordinates: [],
      times: [],
      since: Date.parse("2026-09-26T19:00:00Z"),
      complete: true,
    });
  });

  it("keeps where a window longer than tracks are kept was cut", async () => {
    respond({
      type: "Feature",
      id: "veh-1",
      geometry: null,
      properties: {
        device_id: "veh-1",
        since: "2026-09-26T18:30:00Z",
        timestamps: [],
        speeds: [],
        complete: false,
      },
    });
    await expect(deviceTrail("veh-1", 60)).resolves.toEqual({
      coordinates: [],
      times: [],
      since: Date.parse("2026-09-26T18:30:00Z"),
      complete: false,
    });
  });

  it("refuses a trail whose timestamps do not line up with its coordinates", async () => {
    respond({
      type: "Feature",
      id: "veh-1",
      geometry: { type: "Point", coordinates: [4.3, 52.3] },
      properties: {
        device_id: "veh-1",
        since: "2026-09-26T19:00:00Z",
        timestamps: [],
        speeds: [],
        complete: true,
      },
    });
    await expect(deviceTrail("veh-1", 5)).rejects.toBeInstanceOf(ContractError);
  });

  it("returns occupants with the full count", async () => {
    respond({
      zone_id: "z1",
      occupancy: 250,
      items: [
        {
          device_id: "veh-1",
          position: { lat: 1, lon: 2 },
          recorded_at: "2026-09-26T19:00:00Z",
          speed_mps: null,
          heading_deg: null,
          entered_at: "2026-09-26T18:00:00Z",
          last_seen_at: "2026-09-26T19:00:00Z",
        },
      ],
    });
    const inside = await zoneOccupants("z1");
    expect(inside.occupancy).toBe(250);
    expect(inside.items).toHaveLength(1);
  });

  it("sends If-Match on updates and surfaces 412 as a typed error", async () => {
    const fetchMock = respond(
      {
        type: "https://perimeter.dev/problems/precondition_failed",
        title: "Precondition Failed",
        status: 412,
        detail: "stale",
        code: "precondition_failed",
      },
      { status: 412, headers: { "content-type": "application/problem+json" } },
    );
    const error = await updateZone("z1", { radius_m: 300 }, 3).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(412);
    const init = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit;
    expect((init.headers as Record<string, string>)["if-match"]).toBe('"v3"');
    expect(init.method).toBe("PATCH");
  });

  it("lists live sessions, marking this sign-in's", async () => {
    respond({
      sessions: [
        {
          sid: "0192f7e1-0000-7000-8000-000000000001",
          label: "Firefox · Linux",
          agent: "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
          ip: null,
          replica: "api-2",
          connected_at: "2026-09-27T09:12:44.120000Z",
          current: true,
        },
      ],
    });
    const sessions = await listSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({ label: "Firefox · Linux", ip: null, current: true });
  });

  it("counts a session that is already gone as signed out, and reports real failures", async () => {
    const problem = (status: number) => ({
      status,
      headers: { "content-type": "application/problem+json" },
    });
    const gone = respond({ status: 404, code: "not_found", title: "Not Found" }, problem(404));
    await expect(revokeSession("s-1")).resolves.toBeUndefined();
    const init = (gone.mock.calls[0] as unknown[])[1] as RequestInit;
    expect(init.method).toBe("DELETE");

    respond({ status: 503, code: "unavailable", title: "Service Unavailable" }, problem(503));
    await expect(revokeSession("s-1")).rejects.toBeInstanceOf(ApiError);
  });

  it("rejects a response that does not match the contract, naming the field", async () => {
    respond({ items: [{ ...zone, radius_m: "wide" }], next_cursor: null });
    const error = await listZones().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ContractError);
    expect(String((error as Error).message)).toMatch(/items\.0\.radius_m/);
  });
});
