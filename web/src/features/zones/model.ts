/**
 * Zone data on the client: the cached list, optimistic edits that never race each other, and
 * reconciliation with zone events from other sessions.
 *
 * Every write carries `If-Match: "v<version>"`. Two quick edits from this tab must not collide
 * with each other, so writes to one zone are serialised: while a PATCH is in flight, further
 * edits merge into one pending patch that is sent with the version the first one returns. A 412
 * therefore always means another session changed the zone — the tab shows the latest version and
 * offers to re-apply the local change on top of it. Any failed write is undone on the spot, back
 * to the last version the server confirmed — except one the server only put off (429, too many
 * changes): that one stays and goes out again, with anything edited meanwhile, when the server
 * says it may. A write never carries what the server's version already has, and one that would
 * change nothing is not sent at all: the server counts every write against the account's limit.
 *
 * Zone data reaches the tab by several roads — live events, write answers, re-reads, the list —
 * and not in order: the server's outbox sweeper may publish an older change after a newer one,
 * and an answer can cross an event. The tab keeps the newest version it has seen of every zone,
 * deleted ones included, and nothing older (or anything of a deleted zone) changes the cache.
 */
import type { QueryClient } from "@tanstack/react-query";
import type { ZonePatch } from "@/api/endpoints";
import { isApiError, retryDelayMs } from "@/api/http";
import type { Zone } from "@/api/schemas";
import { withinRadius } from "@/lib/geodesy";

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

/**
 * The round radii from 10 m to 100 km with `figures` significant figures, in order (with two:
 * 10, 11, … 99, 100, 110, … 990, 1000, 1100 …).
 */
function roundRadii(figures: number): number[] {
  const radii: number[] = [];
  const first = 10 ** (figures - 1);
  for (let unit = RADIUS_MIN_M / first; unit * first < RADIUS_MAX_M; unit *= 10) {
    for (let digits = first; digits < first * 10; digits++) radii.push(digits * unit);
  }
  radii.push(RADIUS_MAX_M);
  return radii;
}

const FINE_RADII = roundRadii(2);
const COARSE_RADII = roundRadii(1);

/**
 * The next round radius past `radiusM` in `direction`, within the limits: a step of two
 * significant figures (100 → 110 m, 990 m → 1 km, 1 km → 1.1 km), or of one for a large step
 * (250 → 300 m, 1 → 2 km). Every step lands on another radius, however round the one it left.
 */
export function stepRadius(radiusM: number, direction: 1 | -1, large = false): number {
  const radii = large ? COARSE_RADII : FINE_RADII;
  return direction > 0
    ? (radii.find((r) => r > radiusM) ?? RADIUS_MAX_M)
    : (radii.findLast((r) => r < radiusM) ?? RADIUS_MIN_M);
}

/**
 * The active zones containing a point, the smallest first: the order the map tints devices in
 * (where zones overlap, the smaller one is the more specific).
 */
export function zonesContaining(zones: readonly Zone[], lat: number, lon: number): Zone[] {
  return zones
    .filter((z) => z.is_active && withinRadius(z.center, z.radius_m, { lat, lon }))
    .sort((a, b) => a.radius_m - b.radius_m);
}

export function applyPatch(zone: Zone, patch: ZonePatch): Zone {
  return { ...zone, ...patch } as Zone;
}

/** The part of `patch` that would change `zone`, or `undefined` when none of it would. */
function changesTo(zone: Zone, patch: ZonePatch): ZonePatch | undefined {
  const changes = Object.entries(patch).filter(([key, value]) => {
    if (key !== "center") return zone[key as keyof ZonePatch] !== value;
    const center = value as Zone["center"];
    return center.lat !== zone.center.lat || center.lon !== zone.center.lon;
  });
  return changes.length > 0 ? (Object.fromEntries(changes) as ZonePatch) : undefined;
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

/** Replace the cached zone with `zone`'s id, if it is still there (never brings one back). */
function replaceZone(client: QueryClient, zone: Zone): void {
  writeZones(client, (zones) => zones.map((z) => (z.id === zone.id ? zone : z)));
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

/** What the tab knows of its zones beyond the cached list (kept by {@link ZonePatcher}). */
export interface ZoneLedger {
  /** Local changes not yet confirmed by the server, kept on top of incoming versions. */
  overlay(zoneId: string): ZonePatch | undefined;
  /** Whether `zone` is newer than every version of it seen so far (never, once it is deleted). */
  isNews(zone: Zone): boolean;
  /** The server has shown this version of the zone. */
  saw(zone: Zone): void;
  /** The zone is deleted: no version of it may come back. */
  bury(zoneId: string): void;
}

export class ZonePatcher implements ZoneLedger {
  #client: QueryClient;
  #api: PatchApi;
  #hooks: PatchHooks;
  #pending = new Map<string, ZonePatch>();
  #inflight = new Map<string, ZonePatch>();
  /** The newest version the server confirmed of each zone with local changes: what failure restores. */
  #confirmed = new Map<string, Zone>();
  /** The newest version seen of every zone; a deleted zone's is infinitely new. */
  #versions = new Map<string, number>();
  /** When each zone was first seen, on the ledger's own clock (see {@link stamp}). */
  #firstSeen = new Map<string, number>();
  #clock = 0;
  /** Zones this tab is deleting: nothing announced about them applies until that settles. */
  #deleting = new Set<string>();
  #idle = new Map<string, (() => void)[]>();
  /** Changes the server put off (429), waiting to go out again. */
  #retries = new Map<string, ReturnType<typeof setTimeout>>();

  constructor(client: QueryClient, api: PatchApi, hooks: PatchHooks) {
    this.#client = client;
    this.#api = api;
    this.#hooks = hooks;
  }

  /** Apply `patch` locally now and persist it as soon as no other write to the zone is pending. */
  patch(zoneId: string, patch: ZonePatch): void {
    if (!this.busy(zoneId) && !isDraft(zoneId)) {
      // Nothing of this tab is on the zone yet, so the cached zone is exactly the server's.
      const current = readZones(this.#client).find((z) => z.id === zoneId);
      if (current) this.#confirmed.set(zoneId, current);
    }
    writeZones(this.#client, (zones) =>
      zones.map((z) => (z.id === zoneId ? applyPatch(z, patch) : z)),
    );
    this.#pending.set(zoneId, { ...this.#pending.get(zoneId), ...patch });
    // A drawn zone is saved by its creation request; its edits wait for the real id.
    if (!isDraft(zoneId) && !this.#inflight.has(zoneId)) void this.#flush(zoneId);
  }

  /** The draft `from` was created as `saved`: send the edits made meanwhile against it. */
  rekey(from: string, saved: Zone): void {
    const pending = this.#pending.get(from);
    this.#pending.delete(from);
    this.#settle(from);
    if (!pending) return;
    const to = saved.id;
    if (!this.busy(to)) this.#confirmed.set(to, saved);
    this.#pending.set(to, { ...pending, ...this.#pending.get(to) });
    if (!this.#inflight.has(to)) void this.#flush(to);
  }

  /** Forget queued edits of a draft that will not be saved (its creation failed, or it was deleted). */
  discard(zoneId: string): void {
    this.#pending.delete(zoneId);
    this.#settle(zoneId);
  }

  /**
   * The server has shown this version of the zone (in an event, an answer or the list). Nothing
   * older applies from now on, and a local change to the zone that fails falls back to it.
   */
  saw(zone: Zone): void {
    if (!this.isNews(zone)) return;
    if (!this.#versions.has(zone.id)) this.#firstSeen.set(zone.id, this.#clock);
    this.#versions.set(zone.id, zone.version);
    const confirmed = this.#confirmed.get(zone.id);
    if (confirmed && zone.version > confirmed.version) this.#confirmed.set(zone.id, zone);
  }

  isNews(zone: Zone): boolean {
    return zone.version > this.#known(zone.id);
  }

  /**
   * A mark on the ledger's history, taken when a read of the zone list is sent: zones first seen
   * after it (see {@link seenSince}) may be missing from that list without being gone.
   */
  stamp(): number {
    this.#clock += 1;
    return this.#clock;
  }

  /** Whether the zone was first seen after `stamp` was taken. */
  seenSince(zoneId: string, stamp: number): boolean {
    return (this.#firstSeen.get(zoneId) ?? Number.NEGATIVE_INFINITY) >= stamp;
  }

  /** Whether `zone` is at least as new as every version of it seen so far (never, once deleted). */
  isCurrent(zone: Zone): boolean {
    return zone.version >= this.#known(zone.id);
  }

  bury(zoneId: string): void {
    this.#deleting.delete(zoneId);
    this.#versions.set(zoneId, Number.POSITIVE_INFINITY);
  }

  /** This tab is deleting the zone: late news of it must not bring it back meanwhile. */
  deleting(zoneId: string): void {
    this.#deleting.add(zoneId);
  }

  /**
   * This tab's deletion of the zone did not go through and `zone` is to be shown again. Returns
   * false when the zone's deletion was announced meanwhile (someone else's went through).
   */
  undelete(zone: Zone): boolean {
    this.#deleting.delete(zone.id);
    if (this.#buried(zone.id)) return false;
    this.saw(zone);
    return true;
  }

  #known(zoneId: string): number {
    if (this.#deleting.has(zoneId)) return Number.POSITIVE_INFINITY;
    return this.#versions.get(zoneId) ?? Number.NEGATIVE_INFINITY;
  }

  #buried(zoneId: string): boolean {
    return this.#versions.get(zoneId) === Number.POSITIVE_INFINITY;
  }

  /** Local changes not yet confirmed by the server (kept on top of incoming zone events). */
  overlay(zoneId: string): ZonePatch | undefined {
    const local = { ...this.#inflight.get(zoneId), ...this.#pending.get(zoneId) };
    return Object.keys(local).length > 0 ? local : undefined;
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

  /** The session ended: nothing put off goes out any more. */
  stop(): void {
    for (const timer of this.#retries.values()) clearTimeout(timer);
    this.#retries.clear();
  }

  async #flush(zoneId: string): Promise<void> {
    const pending = this.#pending.get(zoneId);
    if (!pending) {
      this.#confirmed.delete(zoneId);
      this.#settle(zoneId);
      return;
    }
    this.#pending.delete(zoneId);
    const current = readZones(this.#client).find((z) => z.id === zoneId);
    // Only what differs from the server's version goes out: an edit made and undone while
    // another write was on its way changes nothing, and is not sent.
    const confirmed = this.#confirmed.get(zoneId);
    const patch = confirmed ? changesTo(confirmed, pending) : pending;
    if (!current || !patch) return this.#flush(zoneId);
    this.#inflight.set(zoneId, patch);
    try {
      const saved = await this.#api.update(zoneId, patch, current.version);
      this.#inflight.delete(zoneId);
      // Unless a newer change (or the deletion) was announced while this answer was on its way:
      // the cache shows that already, with this tab's waiting edits on top.
      if (this.isCurrent(saved)) {
        this.saw(saved);
        this.#show(saved);
      }
    } catch (error) {
      const wait = retryDelayMs(error);
      if (wait !== null) {
        this.#putOff(zoneId, patch, wait);
        return;
      }
      await this.#recover(zoneId, { ...patch, ...this.#pending.get(zoneId) }, error);
    }
    return this.#flush(zoneId);
  }

  /**
   * The server put the write off (too many changes, for now): it is not a refusal, so nothing is
   * undone. The change stays on the zone and goes out again once the wait is over, together with
   * whatever was edited meanwhile — the zone stays busy, so those edits join it.
   */
  #putOff(zoneId: string, patch: ZonePatch, waitMs: number): void {
    this.#pending.set(zoneId, { ...patch, ...this.#pending.get(zoneId) });
    this.#inflight.set(zoneId, {});
    const timer = setTimeout(() => {
      this.#retries.delete(zoneId);
      this.#inflight.delete(zoneId);
      void this.#flush(zoneId);
    }, waitMs);
    this.#retries.set(zoneId, timer);
  }

  /**
   * A write failed: undo `change` at once, back to the version the server last confirmed, then
   * read the zone to show its latest state. The zone stays busy meanwhile, so edits made in the
   * meantime wait and go out on top of what the server has.
   */
  async #recover(zoneId: string, change: ZonePatch, error: unknown): Promise<void> {
    this.#pending.delete(zoneId);
    this.#inflight.set(zoneId, {});
    try {
      if (isApiError(error, 404)) {
        this.#gone(zoneId);
        return;
      }
      const confirmed = this.#confirmed.get(zoneId);
      const cached = readZones(this.#client).find((z) => z.id === zoneId);
      if (confirmed)
        this.#show({ ...confirmed, occupancy: cached?.occupancy ?? confirmed.occupancy });
      let latest: Zone | null = null;
      try {
        latest = await this.#api.get(zoneId);
      } catch (refetchError) {
        if (isApiError(refetchError, 404)) {
          this.#gone(zoneId);
          return;
        }
      }
      if (this.#buried(zoneId)) {
        this.#gone(zoneId);
        return;
      }
      if (latest && this.isCurrent(latest)) {
        this.saw(latest);
        this.#show(latest);
      }
      if (latest && isApiError(error, 412)) this.#hooks.onConflict({ zone: latest, change });
      else this.#hooks.onError(zoneId, error);
    } finally {
      this.#inflight.delete(zoneId);
    }
  }

  /** Show the server's `zone` in the cache, with the edits still waiting to be sent on top. */
  #show(zone: Zone): void {
    const pending = this.#pending.get(zone.id);
    replaceZone(this.#client, pending ? applyPatch(zone, pending) : zone);
  }

  #gone(zoneId: string): void {
    this.bury(zoneId);
    this.#pending.delete(zoneId);
    removeZone(this.#client, zoneId);
    this.#hooks.onGone(zoneId);
  }

  /** Wake whoever waits for the zone's writes, once none is left. */
  #settle(zoneId: string): void {
    if (this.busy(zoneId)) return;
    for (const resolve of this.#idle.get(zoneId) ?? []) resolve();
    this.#idle.delete(zoneId);
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
  ledger: ZoneLedger,
): boolean {
  if (event.type === "zone.deleted") {
    const existed = readZones(client).some((z) => z.id === event.id);
    ledger.bury(event.id);
    removeZone(client, event.id);
    return existed;
  }
  const incoming = event.zone;
  const cached = readZones(client).find((z) => z.id === incoming.id);
  // An event older than what the tab has seen (the outbox sweeper publishes late ones), a repeat,
  // or one about a zone deleted since, changes nothing.
  if (!ledger.isNews(incoming) || (cached && cached.version >= incoming.version)) return false;
  ledger.saw(incoming);
  const local = ledger.overlay(incoming.id);
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
