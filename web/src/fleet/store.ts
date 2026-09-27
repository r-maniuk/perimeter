/**
 * Live device state, laid out for the GPU.
 *
 * Ten thousand devices update ~3,300 times a second in total. Keeping them as React state or
 * GeoJSON would mean re-serialising the fleet on every frame; instead each device owns one slot in
 * a set of typed arrays (struct of arrays), and one interleaved Float32 "instance" buffer mirrors
 * exactly what the WebGL layer draws. An update touches one slot and marks a dirty range; the
 * layer uploads that range once per frame and the GPU interpolates motion between updates, so the
 * per-frame JavaScript cost does not grow with the fleet.
 *
 * Positions are kept in float64 Web-Mercator on the CPU and written to the GPU relative to a
 * movable origin: float32 alone cannot place a device to the pixel at street zoom (it has ~7
 * significant digits; the world is 5·10⁸ pixels wide at zoom 20).
 */

import { FrameKind, type TileFrame, UNKNOWN_U16 } from "@/lib/frames";
import { type Envelope, envelope, mercatorX, mercatorY, withinRadius } from "@/lib/geodesy";
import { coveredBy, quadkey, quadkeyFor } from "@/lib/tiles";

/** Floats per device in the instance buffer (see {@link InstanceLayout}). */
export const STRIDE = 12;

/** Offsets (in floats) of each attribute inside a device's instance slot. */
export const InstanceLayout = {
  from: 0,
  to: 2,
  time: 4,
  heading: 6,
  speed: 7,
  recorded: 8,
  born: 9,
  color: 10,
  flags: 11,
} as const;

export const Flag = {
  Moving: 1,
  InZone: 2,
} as const;

/** Upper bound on how old a snapshot can be when it arrives (server cache + transit). */
const SNAPSHOT_AGE_MS = 2_000;
const MIN_ANIMATION_S = 0.25;
const MAX_ANIMATION_S = 4;
const FIRST_ANIMATION_S = 1;
/** A jump longer than this is drawn as a jump, not as a 4-second glide across town. */
const TELEPORT_M = 2_000;
const STATIONARY_MPS = 0.5;
const MOVED_M = 3;

export interface ZoneShape {
  id: string;
  lat: number;
  lon: number;
  radiusM: number;
  /** Packed RGBA (see `packRgba`). */
  color: number;
  active: boolean;
}

interface PreparedZone extends ZoneShape {
  box: Envelope;
}

export interface Clock {
  /** Seconds on a monotonic timeline (animation time). */
  now(): number;
}

export const performanceClock: Clock = { now: () => performance.now() / 1000 };

export interface DeviceView {
  id: string;
  lat: number;
  lon: number;
  recordedAt: number;
  speedMps: number | null;
  headingDeg: number | null;
  zoneId: string | null;
  moving: boolean;
}

export class FleetStore {
  leafZoom: number;
  readonly timeBase: number;
  /** Epoch seconds that `recorded` values in the instance buffer are relative to. */
  readonly epochBase: number;

  count = 0;
  #capacity = 0;
  ids: string[] = [];
  #index = new Map<string, number>();
  #keys: string[] = [];

  #fromX = new Float64Array(0);
  #fromY = new Float64Array(0);
  #toX = new Float64Array(0);
  #toY = new Float64Array(0);
  #t0 = new Float64Array(0);
  #dur = new Float64Array(0);
  #lastArrival = new Float64Array(0);
  #interval = new Float64Array(0);
  #lat = new Float64Array(0);
  #lon = new Float64Array(0);
  #recordedAt = new Float64Array(0);
  #speed = new Float32Array(0);
  #heading = new Float32Array(0);
  #zone: (string | null)[] = [];

  /** Interleaved per-device attributes, `STRIDE` floats each. */
  instances = new Float32Array(0);
  #instanceWords = new Uint32Array(0);
  originX = 0.5;
  originY = 0.5;

  /** Inclusive range of device slots changed since the last upload (empty when lo > hi). */
  dirtyLo = Number.POSITIVE_INFINITY;
  dirtyHi = -1;
  /** Bumped when the buffer was reallocated or rewritten wholesale. */
  layoutVersion = 0;
  /** Bumped on every change; lists and counters poll it. */
  version = 0;

  #zones: PreparedZone[] = [];
  #clock: Clock;

  constructor(options: { leafZoom?: number; clock?: Clock; capacity?: number } = {}) {
    this.leafZoom = options.leafZoom ?? 12;
    this.#clock = options.clock ?? performanceClock;
    this.timeBase = this.#clock.now();
    this.epochBase = Math.floor(Date.now() / 1000);
    this.#grow(options.capacity ?? 1024);
  }

  /* ---------------------------------------------------------------- queries */

  indexOf(id: string): number | undefined {
    return this.#index.get(id);
  }

  has(id: string): boolean {
    return this.#index.has(id);
  }

  /** Animation time as the GPU sees it. */
  animationTime(now = this.#clock.now()): number {
    return now - this.timeBase;
  }

  /** Where device `i` is drawn at monotonic time `now` (Mercator). */
  positionAt(i: number, now = this.#clock.now()): [number, number] {
    const dur = this.#dur[i] ?? 0;
    const t = dur > 0 ? Math.min(1, Math.max(0, (now - (this.#t0[i] ?? 0)) / dur)) : 1;
    const fx = this.#fromX[i] ?? 0;
    const fy = this.#fromY[i] ?? 0;
    return [fx + ((this.#toX[i] ?? 0) - fx) * t, fy + ((this.#toY[i] ?? 0) - fy) * t];
  }

  view(i: number): DeviceView | null {
    if (i < 0 || i >= this.count) return null;
    const speed = this.#speed[i] ?? Number.NaN;
    const heading = this.#heading[i] ?? Number.NaN;
    return {
      id: this.ids[i] as string,
      lat: this.#lat[i] ?? 0,
      lon: this.#lon[i] ?? 0,
      recordedAt: this.#recordedAt[i] ?? 0,
      speedMps: Number.isNaN(speed) ? null : speed,
      headingDeg: Number.isNaN(heading) ? null : heading,
      zoneId: this.#zone[i] ?? null,
      moving: ((this.instances[i * STRIDE + InstanceLayout.flags] ?? 0) & Flag.Moving) !== 0,
    };
  }

  viewOf(id: string): DeviceView | null {
    const i = this.#index.get(id);
    return i === undefined ? null : this.view(i);
  }

  /**
   * Nearest device to Mercator point (`x`, `y`) within `radius` Mercator units, as drawn at `now`.
   * `x` may lie in any world copy (a map panned past ±180° reports it unwrapped).
   * A linear scan: ~10k distance checks is a few tens of microseconds, cheaper than maintaining
   * a spatial index under constant motion.
   */
  pick(x: number, y: number, radius: number, now = this.#clock.now()): number | null {
    let best: number | null = null;
    let bestD2 = radius * radius;
    for (let i = 0; i < this.count; i++) {
      const [px, py] = this.positionAt(i, now);
      const dx = foldX(px - x);
      const dy = py - y;
      const d2 = dx * dx + dy * dy;
      if (d2 <= bestD2) {
        bestD2 = d2;
        best = i;
      }
    }
    return best;
  }

  /**
   * Devices whose last reported position lies inside a lon/lat box, and how many of them move.
   * The stream covers whole tiles around the viewport; this is what is actually on screen.
   * No allocation; called about once a second.
   */
  countWithin(
    west: number,
    south: number,
    east: number,
    north: number,
  ): { total: number; moving: number } {
    let total = 0;
    let moving = 0;
    const f = this.instances;
    for (let i = 0; i < this.count; i++) {
      if (!inBox(this.#lat[i] ?? 0, this.#lon[i] ?? 0, west, south, east, north)) continue;
      total++;
      if (((f[i * STRIDE + InstanceLayout.flags] ?? 0) & Flag.Moving) !== 0) moving++;
    }
    return { total, moving };
  }

  /** Ids of devices whose zone tint is `zoneId`. */
  inZone(zoneId: string): string[] {
    const out: string[] = [];
    for (let i = 0; i < this.count; i++)
      if (this.#zone[i] === zoneId) out.push(this.ids[i] as string);
    return out;
  }

  /* ---------------------------------------------------------------- updates */

  /**
   * Apply one decoded tile frame. Per device the newest report wins, whichever frame carried it:
   * a snapshot (read from the database, up to a second old) can arrive after a live frame with a
   * newer position for the same device, and must not move it back.
   *
   * `serverNowMs` (the server's clock) dates a snapshot that lists no devices at all.
   */
  applyFrame(frame: TileFrame, serverNowMs: number = Date.now()): void {
    const now = this.#clock.now();
    if (frame.kind === FrameKind.Snapshot) {
      this.#applySnapshot(frame, now, serverNowMs);
      return;
    }
    for (let k = 0; k < frame.count; k++) this.#upsertFromFrame(frame, k, now);
  }

  #applySnapshot(frame: TileFrame, now: number, serverNowMs: number): void {
    const prefix = quadkey({ x: frame.x, y: frame.y, z: frame.zoom });
    const present = new Set<string>(frame.ids);
    let newest = 0;
    for (let k = 0; k < frame.count; k++) {
      newest = Math.max(newest, frame.baseTimeMs + (frame.deltaMs[k] ?? 0));
    }
    // The snapshot was taken no earlier than its newest report and at most a couple of seconds
    // ago. A device we hold there, missing from it and not updated since, is gone (stale, or it
    // moved away while we were not listening); anything newer arrived after the snapshot.
    const takenAfter = Math.max(newest, serverNowMs - SNAPSHOT_AGE_MS);
    for (let i = this.count - 1; i >= 0; i--) {
      if (
        (this.#keys[i] as string).startsWith(prefix) &&
        !present.has(this.ids[i] as string) &&
        (this.#recordedAt[i] ?? 0) <= takenAfter
      ) {
        this.#remove(i);
      }
    }
    for (let k = 0; k < frame.count; k++) this.#upsertFromFrame(frame, k, now);
  }

  #upsertFromFrame(frame: TileFrame, k: number, now: number): void {
    const speed = frame.speed[k] ?? UNKNOWN_U16;
    const heading = frame.heading[k] ?? UNKNOWN_U16;
    this.upsert(
      frame.ids[k] as string,
      (frame.lat[k] ?? 0) / 1e7,
      (frame.lon[k] ?? 0) / 1e7,
      frame.baseTimeMs + (frame.deltaMs[k] ?? 0),
      speed === UNKNOWN_U16 ? null : speed / 100,
      heading === UNKNOWN_U16 ? null : heading / 100,
      now,
    );
  }

  /** Insert or move one device. Older reports than the one shown are ignored. */
  upsert(
    id: string,
    lat: number,
    lon: number,
    recordedAtMs: number,
    speedMps: number | null,
    headingDeg: number | null,
    now = this.#clock.now(),
  ): void {
    const x = mercatorX(lon);
    const y = mercatorY(lat);
    let i = this.#index.get(id);
    let moved = 0;
    if (i === undefined) {
      i = this.#append(id);
      this.#fromX[i] = x;
      this.#fromY[i] = y;
      this.#dur[i] = 0;
      this.#interval[i] = FIRST_ANIMATION_S;
      this.#write(i, InstanceLayout.born, now - this.timeBase);
    } else {
      if (recordedAtMs <= (this.#recordedAt[i] ?? 0)) return;
      const [cx, cy] = this.positionAt(i, now);
      const gap = now - (this.#lastArrival[i] ?? now);
      const previous = this.#interval[i] ?? FIRST_ANIMATION_S;
      const interval = Math.min(MAX_ANIMATION_S, Math.max(MIN_ANIMATION_S, gap));
      this.#interval[i] = previous * 0.6 + interval * 0.4;
      moved = metersBetween(cx, cy, x, y, lat);
      // Glide from where the device is drawn, the short way (also across the antimeridian).
      this.#fromX[i] = x + foldX(cx - x);
      this.#fromY[i] = cy;
      this.#dur[i] = moved > TELEPORT_M ? 0 : (this.#interval[i] ?? FIRST_ANIMATION_S);
    }
    this.#toX[i] = x;
    this.#toY[i] = y;
    this.#t0[i] = now;
    this.#lastArrival[i] = now;
    this.#lat[i] = lat;
    this.#lon[i] = lon;
    this.#recordedAt[i] = recordedAtMs;
    this.#speed[i] = speedMps ?? Number.NaN;
    this.#heading[i] = headingDeg ?? Number.NaN;
    this.#keys[i] = quadkeyFor(lon, lat, this.leafZoom);
    const moving = speedMps === null ? moved > MOVED_M : speedMps >= STATIONARY_MPS;
    this.#tint(i, lat, lon);

    const base = i * STRIDE;
    const f = this.instances;
    this.#writePosition(i);
    f[base + InstanceLayout.time] = now - this.timeBase;
    f[base + InstanceLayout.time + 1] = this.#dur[i] ?? 0;
    f[base + InstanceLayout.heading] = headingDeg === null ? -1 : (headingDeg * Math.PI) / 180;
    f[base + InstanceLayout.speed] = speedMps ?? -1;
    f[base + InstanceLayout.recorded] = recordedAtMs / 1000 - this.epochBase;
    const flags = (moving ? Flag.Moving : 0) | (this.#zone[i] ? Flag.InZone : 0);
    f[base + InstanceLayout.flags] = flags;
    this.#touch(i);
  }

  /** Keep only devices whose leaf tile lies under one of `prefixes` (the live subscription). */
  retainCoverage(prefixes: readonly string[]): number {
    let removed = 0;
    for (let i = this.count - 1; i >= 0; i--) {
      if (!coveredBy(this.#keys[i] as string, prefixes)) {
        this.#remove(i);
        removed++;
      }
    }
    return removed;
  }

  /** Drop devices whose last report is older than `staleS` on the server's clock. */
  sweepStale(serverNowMs: number, staleS: number): number {
    const cutoff = serverNowMs - staleS * 1000;
    let removed = 0;
    for (let i = this.count - 1; i >= 0; i--) {
      if ((this.#recordedAt[i] ?? 0) < cutoff) {
        this.#remove(i);
        removed++;
      }
    }
    return removed;
  }

  clear(): void {
    this.count = 0;
    this.ids = [];
    this.#keys = [];
    this.#zone = [];
    this.#index.clear();
    this.layoutVersion++;
    this.version++;
    this.dirtyLo = Number.POSITIVE_INFINITY;
    this.dirtyHi = -1;
  }

  /** The server announced a different leaf zoom (`hello.tile_zoom`): re-key every device. */
  setLeafZoom(zoom: number): void {
    if (zoom === this.leafZoom) return;
    this.leafZoom = zoom;
    for (let i = 0; i < this.count; i++) {
      this.#keys[i] = quadkeyFor(this.#lon[i] ?? 0, this.#lat[i] ?? 0, zoom);
    }
  }

  /* ---------------------------------------------------------------- zones */

  setZones(zones: readonly ZoneShape[]): void {
    this.#zones = zones
      .filter((z) => z.active)
      .map((z) => ({ ...z, box: envelope({ lat: z.lat, lon: z.lon }, z.radiusM) }))
      // The smallest zone wins when zones overlap: it is the more specific one.
      .sort((a, b) => a.radiusM - b.radiusM);
    for (let i = 0; i < this.count; i++) {
      this.#tint(i, this.#lat[i] ?? 0, this.#lon[i] ?? 0);
      const base = i * STRIDE + InstanceLayout.flags;
      const flags = (this.instances[base] ?? 0) & ~Flag.InZone;
      this.instances[base] = flags | (this.#zone[i] ? Flag.InZone : 0);
    }
    if (this.count > 0) {
      this.dirtyLo = 0;
      this.dirtyHi = this.count - 1;
    }
    this.version++;
  }

  #tint(i: number, lat: number, lon: number): void {
    const point = { lat, lon };
    for (const zone of this.#zones) {
      if (withinRadius(zone, zone.radiusM, point, zone.box)) {
        this.#zone[i] = zone.id;
        this.#instanceWords[i * STRIDE + InstanceLayout.color] = zone.color;
        return;
      }
    }
    this.#zone[i] = null;
    this.#instanceWords[i * STRIDE + InstanceLayout.color] = 0;
  }

  /* ---------------------------------------------------------------- origin */

  /**
   * Move the origin positions are expressed against. Called by the renderer when the camera has
   * travelled far from it, so offsets stay small and float32 stays sub-pixel precise.
   */
  rebase(x: number, y: number): void {
    this.originX = x;
    this.originY = y;
    for (let i = 0; i < this.count; i++) this.#writePosition(i);
    this.layoutVersion++;
  }

  /**
   * Device `i`'s path relative to the origin, taking the short way around the world: every
   * device lands within half a world of the origin, so the renderer can draw whole-world copies
   * (a map panned past ±180°) and a device across the antimeridian from the camera stays next to
   * it rather than a world away. `from` is placed relative to `to` the same way.
   */
  #writePosition(i: number): void {
    const f = this.instances;
    const base = i * STRIDE;
    const toX = this.#toX[i] ?? 0;
    const to = foldX(toX - this.originX);
    f[base + InstanceLayout.from] = to + foldX((this.#fromX[i] ?? 0) - toX);
    f[base + InstanceLayout.from + 1] = (this.#fromY[i] ?? 0) - this.originY;
    f[base + InstanceLayout.to] = to;
    f[base + InstanceLayout.to + 1] = (this.#toY[i] ?? 0) - this.originY;
  }

  /** Consume the dirty range (the renderer calls this right before uploading). */
  takeDirty(): [number, number] | null {
    if (this.dirtyHi < this.dirtyLo) return null;
    const range: [number, number] = [this.dirtyLo, Math.min(this.dirtyHi, this.count - 1)];
    this.dirtyLo = Number.POSITIVE_INFINITY;
    this.dirtyHi = -1;
    return range[1] >= range[0] ? range : null;
  }

  /* ---------------------------------------------------------------- storage */

  #touch(i: number): void {
    if (i < this.dirtyLo) this.dirtyLo = i;
    if (i > this.dirtyHi) this.dirtyHi = i;
    this.version++;
  }

  #write(i: number, offset: number, value: number): void {
    this.instances[i * STRIDE + offset] = value;
  }

  #append(id: string): number {
    if (this.count === this.#capacity) this.#grow(this.#capacity * 2);
    const i = this.count++;
    this.ids[i] = id;
    this.#index.set(id, i);
    this.#zone[i] = null;
    this.#keys[i] = "";
    return i;
  }

  /** Swap-remove: the last device takes slot `i`, keeping the arrays dense for drawing. */
  #remove(i: number): void {
    const last = this.count - 1;
    const id = this.ids[i] as string;
    this.#index.delete(id);
    if (i !== last) {
      const moved = this.ids[last] as string;
      this.ids[i] = moved;
      this.#index.set(moved, i);
      this.#keys[i] = this.#keys[last] as string;
      this.#zone[i] = this.#zone[last] ?? null;
      for (const array of [
        this.#fromX,
        this.#fromY,
        this.#toX,
        this.#toY,
        this.#t0,
        this.#dur,
        this.#lastArrival,
        this.#interval,
        this.#lat,
        this.#lon,
        this.#recordedAt,
      ]) {
        array[i] = array[last] ?? 0;
      }
      this.#speed[i] = this.#speed[last] ?? Number.NaN;
      this.#heading[i] = this.#heading[last] ?? Number.NaN;
      this.instances.copyWithin(i * STRIDE, last * STRIDE, last * STRIDE + STRIDE);
      this.#touch(i);
    }
    this.ids.length = last;
    this.#keys.length = last;
    this.#zone.length = last;
    this.count = last;
    this.version++;
  }

  #grow(capacity: number): void {
    const grow64 = (a: Float64Array) => {
      const next = new Float64Array(capacity);
      next.set(a.subarray(0, Math.min(a.length, capacity)));
      return next;
    };
    const grow32 = (a: Float32Array) => {
      const next = new Float32Array(capacity);
      next.set(a.subarray(0, Math.min(a.length, capacity)));
      return next;
    };
    this.#fromX = grow64(this.#fromX);
    this.#fromY = grow64(this.#fromY);
    this.#toX = grow64(this.#toX);
    this.#toY = grow64(this.#toY);
    this.#t0 = grow64(this.#t0);
    this.#dur = grow64(this.#dur);
    this.#lastArrival = grow64(this.#lastArrival);
    this.#interval = grow64(this.#interval);
    this.#lat = grow64(this.#lat);
    this.#lon = grow64(this.#lon);
    this.#recordedAt = grow64(this.#recordedAt);
    this.#speed = grow32(this.#speed);
    this.#heading = grow32(this.#heading);
    const instances = new Float32Array(capacity * STRIDE);
    instances.set(this.instances.subarray(0, Math.min(this.instances.length, instances.length)));
    this.instances = instances;
    this.#instanceWords = new Uint32Array(instances.buffer);
    this.#capacity = capacity;
    this.layoutVersion++;
  }

  get capacity(): number {
    return this.#capacity;
  }
}

/**
 * Is (lat, lon) inside a lon/lat box? The box may be unwrapped past ±180 (as map bounds are when
 * the view crosses the antimeridian); the point is brought into the box's world copy first.
 */
export function inBox(
  lat: number,
  lon: number,
  west: number,
  south: number,
  east: number,
  north: number,
): boolean {
  if (lat < south || lat > north) return false;
  if (east - west >= 360) return true;
  const shifted = west + ((((lon - west) % 360) + 360) % 360);
  return shifted <= east;
}

/** A Mercator x difference folded into [-0.5, 0.5]: the short way around the world. */
export function foldX(dx: number): number {
  return dx - Math.round(dx);
}

/** Metres between two Mercator points near latitude `lat` (display heuristics only). */
function metersBetween(x1: number, y1: number, x2: number, y2: number, lat: number): number {
  const scale = 2 * Math.PI * 6378137 * Math.cos((lat * Math.PI) / 180);
  return Math.hypot(foldX(x2 - x1), y2 - y1) * scale;
}
