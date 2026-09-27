/**
 * Zone data on the client: the cached list, optimistic edits that never race each other, and
 * reconciliation with zone events from other sessions.
 *
 * Every write carries `If-Match: "v<version>"`. Two quick edits from this tab must not collide
 * with each other, so writes to one zone are serialised: while a PATCH is in flight, further
 * edits merge into one pending patch that is sent with the version the first one returns. A 412
 * therefore always means another session changed the zone — the tab shows the latest version and
 * offers to re-apply the local change on top of it.
 */
import type { QueryClient } from "@tanstack/react-query";
import type { ZonePatch } from "@/api/endpoints";
import { isApiError } from "@/api/http";
import type { Zone } from "@/api/schemas";

export const ZONES_KEY = ["zones"] as const;

/** Swatch order validated for colour-vision separation between neighbours, on both themes. */
export const ZONE_SWATCHES = [
  { color: "#6d5dfc", name: "Violet" },
  { color: "#e05555", name: "Red" },
  { color: "#2f7fe0", name: "Blue" },
  { color: "#3f9a2e", name: "Green" },
  { color: "#d4508a", name: "Magenta" },
  { color: "#bf7a00", name: "Amber" },
  { color: "#0e9679", name: "Aqua" },
  { color: "#e0662a", name: "Orange" },
] as const;

export const DWELL_PRESETS = [
  { label: "Off", value: null },
  { label: "1 min", value: 60 },
  { label: "5 min", value: 300 },
  { label: "15 min", value: 900 },
] as const;

export const RADIUS_MIN_M = 10;
export const RADIUS_MAX_M = 100_000;

/** The colour a new zone starts with: the first swatch not used yet, in validated order. */
export function nextSwatch(zones: readonly Zone[]): string {
  const used = new Set(zones.map((z) => z.color.toLowerCase()));
  const free = ZONE_SWATCHES.find((s) => !used.has(s.color));
  return (free ?? ZONE_SWATCHES[zones.length % ZONE_SWATCHES.length] ?? ZONE_SWATCHES[0]).color;
}

export function nextZoneName(zones: readonly Zone[]): string {
  const taken = new Set(zones.map((z) => z.name));
  for (let n = zones.length + 1; ; n++) {
    const name = `Zone ${n}`;
    if (!taken.has(name)) return name;
  }
}

/** Zones drawn but not yet confirmed by the server carry a local id with this prefix. */
export const DRAFT_PREFIX = "draft-";

export function isDraft(id: string): boolean {
  return id.startsWith(DRAFT_PREFIX);
}

export function clampRadius(radiusM: number): number {
  return Math.min(RADIUS_MAX_M, Math.max(RADIUS_MIN_M, radiusM));
}

export function applyPatch(zone: Zone, patch: ZonePatch): Zone {
  return { ...zone, ...patch } as Zone;
}

export function readZones(client: QueryClient): Zone[] {
  return client.getQueryData<Zone[]>(ZONES_KEY) ?? [];
}

/**
 * Update the cached zone list in place. Until the list has been fetched there is nothing to
 * update: writing a partial list (one zone from an event, say) would look like a complete, fresh
 * answer and suppress the real fetch, so such writes are dropped — the fetch will include them.
 */
export function writeZones(client: QueryClient, update: (zones: Zone[]) => Zone[]): void {
  client.setQueryData<Zone[]>(ZONES_KEY, (zones) =>
    zones === undefined ? undefined : update(zones),
  );
}

export function upsertZone(client: QueryClient, zone: Zone): void {
  writeZones(client, (zones) => {
    const index = zones.findIndex((z) => z.id === zone.id);
    if (index === -1) return [zone, ...zones];
    const next = zones.slice();
    next[index] = zone;
    return next;
  });
}

export function removeZone(client: QueryClient, id: string): void {
  writeZones(client, (zones) => zones.filter((z) => z.id !== id));
}

export interface PatchApi {
  update(id: string, patch: ZonePatch, version: number): Promise<Zone>;
  get(id: string): Promise<Zone>;
}

export interface PatchConflict {
  zone: Zone;
  /** The local change that did not apply; re-send with `patcher.patch(zone.id, change)`. */
  change: ZonePatch;
}

export interface PatchHooks {
  onConflict(conflict: PatchConflict): void;
  onGone(zoneId: string): void;
  onError(zoneId: string, error: unknown): void;
}

export class ZonePatcher {
  #client: QueryClient;
  #api: PatchApi;
  #hooks: PatchHooks;
  #pending = new Map<string, ZonePatch>();
  #inflight = new Map<string, ZonePatch>();
  #idle = new Map<string, (() => void)[]>();

  constructor(client: QueryClient, api: PatchApi, hooks: PatchHooks) {
    this.#client = client;
    this.#api = api;
    this.#hooks = hooks;
  }

  /** Apply `patch` locally now and persist it as soon as no other write to the zone is pending. */
  patch(zoneId: string, patch: ZonePatch): void {
    writeZones(this.#client, (zones) =>
      zones.map((z) => (z.id === zoneId ? applyPatch(z, patch) : z)),
    );
    this.#pending.set(zoneId, { ...this.#pending.get(zoneId), ...patch });
    // A drawn zone is saved by its creation request; its edits wait for the real id.
    if (!isDraft(zoneId) && !this.#inflight.has(zoneId)) void this.#flush(zoneId);
  }

  /** The draft `from` was created as `to`: send the edits made meanwhile against the real zone. */
  rekey(from: string, to: string): void {
    const pending = this.#pending.get(from);
    this.#pending.delete(from);
    if (!pending) return;
    this.#pending.set(to, { ...pending, ...this.#pending.get(to) });
    if (!this.#inflight.has(to)) void this.#flush(to);
  }

  /** Forget queued edits of a draft whose creation failed. */
  discard(zoneId: string): void {
    this.#pending.delete(zoneId);
  }

  /** Local changes not yet confirmed by the server (kept on top of incoming zone events). */
  overlay(zoneId: string): ZonePatch | undefined {
    const inflight = this.#inflight.get(zoneId);
    const pending = this.#pending.get(zoneId);
    if (!inflight && !pending) return undefined;
    return { ...inflight, ...pending };
  }

  busy(zoneId: string): boolean {
    return this.#inflight.has(zoneId) || this.#pending.has(zoneId);
  }

  /** Resolves once every queued write for the zone has settled. */
  settled(zoneId: string): Promise<void> {
    if (!this.busy(zoneId)) return Promise.resolve();
    return new Promise((resolve) => {
      this.#idle.set(zoneId, [...(this.#idle.get(zoneId) ?? []), resolve]);
    });
  }

  async #flush(zoneId: string): Promise<void> {
    const patch = this.#pending.get(zoneId);
    if (!patch) {
      for (const resolve of this.#idle.get(zoneId) ?? []) resolve();
      this.#idle.delete(zoneId);
      return;
    }
    this.#pending.delete(zoneId);
    this.#inflight.set(zoneId, patch);
    const current = readZones(this.#client).find((z) => z.id === zoneId);
    if (!current) {
      this.#inflight.delete(zoneId);
      return this.#flush(zoneId);
    }
    try {
      const saved = await this.#api.update(zoneId, patch, current.version);
      this.#inflight.delete(zoneId);
      const pending = this.#pending.get(zoneId);
      upsertZone(this.#client, pending ? applyPatch(saved, pending) : saved);
    } catch (error) {
      this.#inflight.delete(zoneId);
      const change = { ...patch, ...this.#pending.get(zoneId) };
      this.#pending.delete(zoneId);
      if (isApiError(error, 404)) {
        removeZone(this.#client, zoneId);
        this.#hooks.onGone(zoneId);
      } else {
        let latest: Zone | null = null;
        try {
          latest = await this.#api.get(zoneId);
          upsertZone(this.#client, latest);
        } catch (refetchError) {
          if (isApiError(refetchError, 404)) {
            removeZone(this.#client, zoneId);
            this.#hooks.onGone(zoneId);
          }
        }
        if (latest && isApiError(error, 412)) {
          this.#hooks.onConflict({ zone: latest, change });
        } else if (!isApiError(error, 404)) {
          this.#hooks.onError(zoneId, error);
        }
      }
    }
    return this.#flush(zoneId);
  }
}

/**
 * Fold a zone event (from any session, this one included) into the cache. Returns whether the
 * zone visibly changed because of someone else, which the map animates.
 */
export function applyZoneEvent(
  client: QueryClient,
  event:
    | { type: "zone.created" | "zone.updated"; zone: Zone }
    | { type: "zone.deleted"; id: string },
  overlay: (zoneId: string) => ZonePatch | undefined,
): boolean {
  if (event.type === "zone.deleted") {
    const existed = readZones(client).some((z) => z.id === event.id);
    removeZone(client, event.id);
    return existed;
  }
  const incoming = event.zone;
  const cached = readZones(client).find((z) => z.id === incoming.id);
  if (cached && cached.version >= incoming.version) return false;
  const local = overlay(incoming.id);
  const next = local ? applyPatch(incoming, local) : incoming;
  const merged =
    cached?.occupancy !== undefined && next.occupancy === undefined
      ? { ...next, occupancy: cached.occupancy }
      : next;
  upsertZone(client, merged);
  if (!cached) return true;
  return (
    !local &&
    (cached.center.lat !== incoming.center.lat ||
      cached.center.lon !== incoming.center.lon ||
      cached.radius_m !== incoming.radius_m ||
      cached.is_active !== incoming.is_active)
  );
}

/** Occupancy moves with enter/exit alerts between list refreshes. */
export function bumpOccupancy(client: QueryClient, zoneId: string, delta: number): void {
  writeZones(client, (zones) =>
    zones.map((z) =>
      z.id === zoneId && z.occupancy !== undefined
        ? { ...z, occupancy: Math.max(0, z.occupancy + delta) }
        : z,
    ),
  );
}
