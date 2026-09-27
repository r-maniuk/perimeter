import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import type { Zone } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { useNotices } from "@/features/shell/notices";
import { useUi } from "@/state/ui";
import { createDrawnZone, deleteZone } from "./actions";
import { applyZoneEvent, isDraft, readZones, ZONES_KEY, ZonePatcher } from "./model";

const api = vi.hoisted(() => ({
  createZone: vi.fn(),
  deleteZone: vi.fn(),
  getZone: vi.fn(),
  listZones: vi.fn(),
  updateZone: vi.fn(),
}));
const runtime = vi.hoisted(() => ({ patcher: null as unknown }));

vi.mock("@/api/endpoints", () => api);
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
    version: 3,
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

let patcher: ZonePatcher;

beforeEach(() => {
  patcher = new ZonePatcher(
    queryClient,
    { update: api.updateZone, get: api.getZone },
    { onConflict: vi.fn(), onGone: vi.fn(), onError: vi.fn() },
  );
  runtime.patcher = patcher;
  queryClient.setQueryData(ZONES_KEY, [zone()]);
  useUi.getState().select({ kind: "zone", id: "z1" });
});

afterEach(() => {
  queryClient.clear();
  for (const mock of Object.values(api)) mock.mockReset();
  useNotices.setState({ notices: [] });
  useUi.getState().select(null);
});

const conflict = () => new ApiError(412, { code: "precondition_failed" }, null);

describe("deleting a zone", () => {
  it("removes it at once and sends the version it was deleted at", async () => {
    api.deleteZone.mockResolvedValue(undefined);
    await deleteZone(zone());
    expect(api.deleteZone).toHaveBeenCalledWith("z1", 3);
    expect(readZones(queryClient)).toEqual([]);
    expect(useUi.getState().selection).toBeNull();
    expect(useNotices.getState().notices).toEqual([]);
  });

  it("after a conflict shows the version another session saved, not its stale copy", async () => {
    api.deleteZone.mockRejectedValue(conflict());
    api.getZone.mockResolvedValue(zone({ radius_m: 800, name: "De Dam", version: 4 }));
    await deleteZone(zone());
    expect(api.getZone).toHaveBeenCalledWith("z1");
    expect(readZones(queryClient)).toEqual([zone({ radius_m: 800, name: "De Dam", version: 4 })]);
    expect(useUi.getState().selection).toEqual({ kind: "zone", id: "z1" });
    expect(useNotices.getState().notices).toEqual([
      expect.objectContaining({ tone: "warning", title: "Zone changed in another session" }),
    ]);
    // What the tab shows now is what the next deletion must name.
    expect(patcher.isNews(zone({ version: 4 }))).toBe(false);
  });

  it("leaves a zone deleted when another session deleted it during the conflict", async () => {
    api.deleteZone.mockRejectedValue(conflict());
    api.getZone.mockRejectedValue(new ApiError(404, { code: "not_found" }, null));
    await deleteZone(zone());
    expect(readZones(queryClient)).toEqual([]);
    expect(useNotices.getState().notices).toEqual([]);
  });

  it("puts the zone back as it was when the deletion fails otherwise", async () => {
    api.deleteZone.mockRejectedValue(new ApiError(503, { code: "unavailable" }, 1));
    await deleteZone(zone());
    expect(api.getZone).not.toHaveBeenCalled();
    expect(readZones(queryClient)).toEqual([zone()]);
    expect(useNotices.getState().notices).toEqual([
      expect.objectContaining({ tone: "error", title: "Couldn't delete the zone" }),
    ]);
  });

  it("lets no late news of the zone bring it back while the deletion is on its way", async () => {
    const answer = deferred<void>();
    api.deleteZone.mockReturnValue(answer.promise);
    const deleting = deleteZone(zone());
    await vi.waitFor(() => expect(api.deleteZone).toHaveBeenCalled());
    for (const version of [2, 3]) {
      applyZoneEvent(queryClient, { type: "zone.updated", zone: zone({ version }) }, patcher);
    }
    expect(readZones(queryClient)).toEqual([]);
    answer.resolve();
    await deleting;
    applyZoneEvent(queryClient, { type: "zone.updated", zone: zone({ version: 4 }) }, patcher);
    expect(readZones(queryClient)).toEqual([]);
  });
});

describe("deleting a zone that is still being created", () => {
  const SHAPE = { lat: 52.36, lon: 4.9, radiusM: 250 };

  async function drawing() {
    const created = deferred<Zone>();
    api.createZone.mockReturnValue(created.promise);
    const creating = createDrawnZone(SHAPE);
    await vi.waitFor(() => expect(api.createZone).toHaveBeenCalled());
    const draft = readZones(queryClient).find((z) => isDraft(z.id));
    if (!draft) throw new Error("no draft on the map");
    return { created, creating, draft };
  }

  it("deletes the zone its creation makes, so the deletion sticks", async () => {
    const { created, creating, draft } = await drawing();
    expect(useUi.getState().selection).toEqual({ kind: "zone", id: draft.id });
    const deleting = deleteZone(draft);
    expect(readZones(queryClient).map((z) => z.id)).toEqual(["z1"]);
    expect(useUi.getState().selection).toBeNull();
    api.deleteZone.mockResolvedValue(undefined);
    created.resolve(zone({ id: "z9", name: draft.name, version: 1 }));
    await Promise.all([creating, deleting]);
    expect(api.deleteZone).toHaveBeenCalledWith("z9", 1);
    expect(readZones(queryClient).map((z) => z.id)).toEqual(["z1"]);
    expect(useUi.getState().selection).toBeNull();
    expect(useNotices.getState().notices).toEqual([]);
  });

  it("keeps it deleted when its creation is announced before the answer arrives", async () => {
    const { created, creating, draft } = await drawing();
    const deleting = deleteZone(draft);
    const saved = zone({ id: "z9", name: draft.name, version: 1 });
    applyZoneEvent(queryClient, { type: "zone.created", zone: saved }, patcher);
    api.deleteZone.mockResolvedValue(undefined);
    created.resolve(saved);
    await Promise.all([creating, deleting]);
    applyZoneEvent(queryClient, { type: "zone.created", zone: saved }, patcher);
    expect(readZones(queryClient).map((z) => z.id)).toEqual(["z1"]);
  });

  it("ends quietly when the creation of a deleted draft fails", async () => {
    const { created, creating, draft } = await drawing();
    const deleting = deleteZone(draft);
    created.reject(new ApiError(503, { code: "unavailable" }, 1));
    await Promise.all([creating, deleting]);
    expect(api.deleteZone).not.toHaveBeenCalled();
    expect(useNotices.getState().notices).toEqual([]);
    expect(readZones(queryClient).map((z) => z.id)).toEqual(["z1"]);
  });

  it("keeps edits made to the draft in view while they are saved on the created zone", async () => {
    const { created, creating, draft } = await drawing();
    patcher.patch(draft.id, { name: "Depot" });
    api.updateZone.mockReturnValue(new Promise(() => {}));
    created.resolve(zone({ id: "z9", name: draft.name, version: 1 }));
    await creating;
    expect(readZones(queryClient).find((z) => z.id === "z9")?.name).toBe("Depot");
    expect(api.updateZone).toHaveBeenCalledWith("z9", { name: "Depot" }, 1);
    expect(useUi.getState().selection).toEqual({ kind: "zone", id: "z9" });
  });
});
