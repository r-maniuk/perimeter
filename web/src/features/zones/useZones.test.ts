import { afterEach, describe, expect, it, vi } from "vitest";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { readZones, ZONES_KEY, ZonePatcher } from "./model";
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

  it("remembers the versions it read, so a late event cannot set them back", async () => {
    const ledger = patcher();
    api.listZones.mockResolvedValue([zone({ version: 6 })]);
    await queryClient.fetchQuery(zonesQuery);
    expect(ledger.isNews(zone({ version: 6 }))).toBe(false);
    expect(ledger.isNews(zone({ version: 7 }))).toBe(true);
  });
});
