import { afterEach, describe, expect, it, vi } from "vitest";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { applyZoneEvent, readZones, upsertZone, ZONES_KEY, ZonePatcher } from "./model";
import { zonesQuery } from "./useZones";

const api = vi.hoisted(() => ({ listZones: vi.fn() }));
const runtime = vi.hoisted(() => ({ patcher: null as unknown }));

vi.mock("@/api/endpoints", () => ({ listZones: api.listZones }));
vi.mock("@/app/runtime", () => ({ getRuntime: () => runtime }));

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
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function patcher(): ZonePatcher {
  const hooks = { onConflict: vi.fn(), onGone: vi.fn(), onError: vi.fn() };
  const created = new ZonePatcher(queryClient, { update: vi.fn(), get: vi.fn() }, hooks);
  runtime.patcher = created;
  return created;
}

afterEach(() => {
  queryClient.clear();
  api.listZones.mockReset();
});

describe("refreshing the zone list", () => {
  it("keeps what live events brought while the list was on its way", async () => {
    const ledger = patcher();
    const renamed = zone({ name: "De Dam", version: 5, occupancy: 2 });
    queryClient.setQueryData(ZONES_KEY, [zone({ id: "draft-1", version: 0 }), renamed]);
    ledger.saw(renamed);
    ledger.bury("z2");
    api.listZones.mockResolvedValue([
      zone({ version: 4, occupancy: 9 }),
      zone({ id: "z2", name: "Depot" }),
      zone({ id: "z3", name: "Harbour" }),
    ]);
    // A refresh of a list already in the cache (the periodic one, or after a reconnect).
    await queryClient.fetchQuery({ ...zonesQuery, staleTime: 0 });
    expect(readZones(queryClient)).toEqual([
      zone({ id: "draft-1", version: 0 }),
      // The newer version stays, with the list's fresher count; the deleted zone stays deleted.
      { ...renamed, occupancy: 9 },
      zone({ id: "z3", name: "Harbour" }),
    ]);
  });

  it("keeps zones created while the list was on its way, which it could not show yet", async () => {
    const ledger = patcher();
    const known = zone();
    queryClient.setQueryData(ZONES_KEY, [known]);
    ledger.saw(known);
    const answer = deferred<Zone[]>();
    api.listZones.mockReturnValue(answer.promise);
    const refresh = queryClient.fetchQuery({ ...zonesQuery, staleTime: 0 });
    // The server has read the list. Then this tab's own creation is answered, and another
    // session's is announced live.
    const mine = zone({ id: "z-mine", name: "Depot" });
    ledger.saw(mine);
    upsertZone(queryClient, mine);
    const theirs = zone({ id: "z-theirs", name: "Harbour" });
    applyZoneEvent(queryClient, { type: "zone.created", zone: theirs }, ledger);
    answer.resolve([known]);
    await refresh;
    expect(readZones(queryClient).map((z) => z.id)).toEqual(["z-theirs", "z-mine", "z1"]);
  });

  it("drops a zone missing from a list asked for after the tab knew of it", async () => {
    const ledger = patcher();
    const gone = zone({ id: "z-gone", name: "Depot" });
    queryClient.setQueryData(ZONES_KEY, [gone, zone()]);
    ledger.saw(gone);
    ledger.saw(zone());
    // Deleted while this tab missed the announcement (its socket was down, say).
    api.listZones.mockResolvedValue([zone()]);
    await queryClient.fetchQuery({ ...zonesQuery, staleTime: 0 });
    expect(readZones(queryClient).map((z) => z.id)).toEqual(["z1"]);
  });

  it("remembers the versions it read, so a late event cannot set them back", async () => {
    const ledger = patcher();
    api.listZones.mockResolvedValue([zone({ version: 6 })]);
    await queryClient.fetchQuery(zonesQuery);
    expect(ledger.isNews(zone({ version: 6 }))).toBe(false);
    expect(ledger.isNews(zone({ version: 7 }))).toBe(true);
  });
});
