import { describe, expect, it } from "vitest";
import { hexToRgba, packRgba } from "@/lib/color";
import { decodeTile, FrameKind } from "@/lib/frames";
import { destination, mercatorX, mercatorY } from "@/lib/geodesy";
import { quadkeyFor, tileFor } from "@/lib/tiles";
import { encodeTile } from "@/test/frames";
import { Flag, FleetStore, InstanceLayout, STRIDE } from "./store";

class ManualClock {
  t = 1_000;
  now = () => this.t;
}

const DAM = { lat: 52.3731, lon: 4.8926 };
const T0 = 1_790_000_000_000;

function setup(capacity = 4) {
  const clock = new ManualClock();
  const store = new FleetStore({ clock, capacity });
  const slot = (id: string) => {
    const i = store.indexOf(id);
    if (i === undefined) throw new Error(`no ${id}`);
    return Array.from(store.instances.subarray(i * STRIDE, i * STRIDE + STRIDE));
  };
  const words = (id: string) => {
    const i = store.indexOf(id) as number;
    return new Uint32Array(store.instances.buffer)[i * STRIDE + InstanceLayout.color];
  };
  return { clock, store, slot, words };
}

describe("updates", () => {
  it("places a new device without animation and fades it in from its first sight", () => {
    const { store, slot, clock } = setup();
    store.upsert("veh-1", DAM.lat, DAM.lon, T0, 12.5, 90);
    const s = slot("veh-1");
    const x = mercatorX(DAM.lon) - store.originX;
    const y = mercatorY(DAM.lat) - store.originY;
    expect(s[InstanceLayout.from]).toBeCloseTo(x, 7);
    expect(s[InstanceLayout.to + 1]).toBeCloseTo(y, 7);
    expect(s[InstanceLayout.time + 1]).toBe(0);
    expect(s[InstanceLayout.born]).toBe(clock.t - store.timeBase);
    expect(s[InstanceLayout.heading]).toBeCloseTo(Math.PI / 2, 6);
    expect(s[InstanceLayout.speed]).toBe(12.5);
    expect(s[InstanceLayout.flags]).toBe(Flag.Moving);
    expect(store.view(0)).toMatchObject({ id: "veh-1", speedMps: 12.5, headingDeg: 90 });
  });

  it("glides from where the device is drawn to its new position", () => {
    const { store, clock } = setup();
    store.upsert("veh-1", DAM.lat, DAM.lon, T0, 10, 0);
    clock.t += 3;
    const north = destination(DAM, 0, 30);
    store.upsert("veh-1", north.lat, north.lon, T0 + 3_000, 10, 0);
    const i = store.indexOf("veh-1") as number;
    const start = store.positionAt(i, clock.t);
    expect(start[1]).toBeCloseTo(mercatorY(DAM.lat), 12);
    const halfway = store.positionAt(i, clock.t + 0.5 * (0.6 + 0.4 * 3));
    expect(halfway[1]).toBeCloseTo((mercatorY(DAM.lat) + mercatorY(north.lat)) / 2, 10);
    expect(store.positionAt(i, clock.t + 100)[1]).toBeCloseTo(mercatorY(north.lat), 12);
  });

  it("continues smoothly from a mid-flight position when an update arrives early", () => {
    const { store, clock } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 10, 0);
    clock.t += 1;
    const p1 = destination(DAM, 90, 10);
    store.upsert("a", p1.lat, p1.lon, T0 + 1_000, 10, 90);
    clock.t += 0.3;
    const i = store.indexOf("a") as number;
    const drawn = store.positionAt(i, clock.t);
    const p2 = destination(p1, 90, 10);
    store.upsert("a", p2.lat, p2.lon, T0 + 2_000, 10, 90);
    expect(store.positionAt(i, clock.t)).toEqual(drawn);
  });

  it("ignores reports older than the one shown", () => {
    const { store } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0 + 5_000, null, null);
    store.upsert("a", 10, 10, T0 + 4_000, null, null);
    store.upsert("a", 10, 10, T0 + 5_000, null, null);
    expect(store.view(0)?.lat).toBe(DAM.lat);
  });

  it("jumps instead of gliding across town", () => {
    const { store, clock } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 10, 0);
    clock.t += 1;
    const far = destination(DAM, 45, 5_000);
    store.upsert("a", far.lat, far.lon, T0 + 1_000, 10, 45);
    expect(store.instances[InstanceLayout.time + 1]).toBe(0);
    expect(store.positionAt(0, clock.t)[0]).toBeCloseTo(mercatorX(far.lon), 12);
  });

  it("marks devices that do not report speed as moving only when they actually moved", () => {
    const { store, clock } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, null, null);
    expect(store.view(0)?.moving).toBe(false);
    clock.t += 3;
    const p = destination(DAM, 0, 20);
    store.upsert("a", p.lat, p.lon, T0 + 3_000, null, null);
    expect(store.view(0)?.moving).toBe(true);
    store.upsert("b", DAM.lat, DAM.lon, T0, 0.2, 10);
    expect(store.viewOf("b")?.moving).toBe(false);
  });

  it("grows past its initial capacity without losing data", () => {
    const { store } = setup(2);
    for (let k = 0; k < 50; k++) store.upsert(`d${k}`, DAM.lat + k / 1e4, DAM.lon, T0 + k, 1, 1);
    expect(store.count).toBe(50);
    expect(store.capacity).toBeGreaterThanOrEqual(50);
    expect(store.viewOf("d0")?.lat).toBe(DAM.lat);
    expect(store.viewOf("d49")?.recordedAt).toBe(T0 + 49);
  });
});

describe("frames", () => {
  it("applies live frames point by point", () => {
    const { store } = setup();
    const tile = tileFor(DAM.lon, DAM.lat, 12);
    store.applyFrame(
      decodeTile(
        encodeTile(FrameKind.Live, 12, tile.x, tile.y, [
          { deviceId: "veh-1", lat: DAM.lat, lon: DAM.lon, recordedAtMs: T0, speedMps: 5 },
          { deviceId: "ped-2", lat: DAM.lat + 0.001, lon: DAM.lon, recordedAtMs: T0 + 10 },
        ]),
      ),
    );
    expect(store.count).toBe(2);
    expect(store.viewOf("veh-1")).toMatchObject({ speedMps: 5, headingDeg: null });
    expect(store.viewOf("ped-2")?.recordedAt).toBe(T0 + 10);
  });

  it("treats a snapshot as the whole truth for its tile", () => {
    const { store } = setup();
    const inside = destination(DAM, 10, 100);
    const elsewhere = { lat: 48.8566, lon: 2.3522 };
    store.upsert("stays", DAM.lat, DAM.lon, T0, 1, 1);
    store.upsert("gone", inside.lat, inside.lon, T0, 1, 1);
    store.upsert("paris", elsewhere.lat, elsewhere.lon, T0, 1, 1);
    const prefix = tileFor(DAM.lon, DAM.lat, 9);
    store.applyFrame(
      decodeTile(
        encodeTile(FrameKind.Snapshot, 9, prefix.x, prefix.y, [
          { deviceId: "stays", lat: DAM.lat, lon: DAM.lon, recordedAtMs: T0 + 1_000 },
          { deviceId: "new", lat: DAM.lat, lon: DAM.lon + 0.01, recordedAtMs: T0 + 1_000 },
        ]),
      ),
      T0 + 1_500,
    );
    expect(store.ids.slice().sort()).toEqual(["new", "paris", "stays"]);
    expect(store.viewOf("stays")?.recordedAt).toBe(T0 + 1_000);
  });

  it("never lets an older snapshot undo newer live positions", () => {
    const { store } = setup();
    const tile = tileFor(DAM.lon, DAM.lat, 12);
    const moved = destination(DAM, 90, 40);
    // Live frame first, with a newer report than the snapshot that arrives after it.
    store.applyFrame(
      decodeTile(
        encodeTile(FrameKind.Live, 12, tile.x, tile.y, [
          { deviceId: "veh-1", lat: moved.lat, lon: moved.lon, recordedAtMs: T0 + 3_000 },
          { deviceId: "veh-2", lat: DAM.lat, lon: DAM.lon, recordedAtMs: T0 + 2_900 },
        ]),
      ),
      T0 + 3_100,
    );
    store.applyFrame(
      decodeTile(
        encodeTile(FrameKind.Snapshot, 12, tile.x, tile.y, [
          { deviceId: "veh-1", lat: DAM.lat, lon: DAM.lon, recordedAtMs: T0 },
        ]),
      ),
      T0 + 3_200,
    );
    // veh-1 keeps its newer position; veh-2, missing from the older snapshot, is kept too.
    expect(store.viewOf("veh-1")?.recordedAt).toBe(T0 + 3_000);
    expect(store.viewOf("veh-1")?.lon).toBeCloseTo(moved.lon, 6);
    expect(store.has("veh-2")).toBe(true);
  });

  it("drops devices an empty snapshot says are gone, unless they reported since", () => {
    const { store } = setup();
    const tile = tileFor(DAM.lon, DAM.lat, 12);
    store.upsert("old", DAM.lat, DAM.lon, T0, 1, 1);
    store.upsert("fresh", DAM.lat, DAM.lon, T0 + 9_500, 1, 1);
    store.applyFrame(
      decodeTile(encodeTile(FrameKind.Snapshot, 12, tile.x, tile.y, [])),
      T0 + 10_000,
    );
    expect(store.ids).toEqual(["fresh"]);
  });
});

describe("pruning", () => {
  it("keeps only devices under the subscribed prefixes", () => {
    const { store } = setup();
    store.upsert("ams", DAM.lat, DAM.lon, T0, 1, 1);
    store.upsert("paris", 48.8566, 2.3522, T0, 1, 1);
    const removed = store.retainCoverage([quadkeyFor(DAM.lon, DAM.lat, 8)]);
    expect(removed).toBe(1);
    expect(store.ids).toEqual(["ams"]);
  });

  it("drops devices that went silent for longer than the stale window", () => {
    const { store } = setup();
    store.upsert("old", DAM.lat, DAM.lon, T0, 1, 1);
    store.upsert("fresh", DAM.lat, DAM.lon, T0 + 590_000, 1, 1);
    expect(store.sweepStale(T0 + 601_000, 600)).toBe(1);
    expect(store.ids).toEqual(["fresh"]);
  });

  it("swap-removes and keeps every index consistent", () => {
    const { store, slot } = setup();
    for (const [k, id] of ["a", "b", "c", "d"].entries()) {
      store.upsert(id, DAM.lat + k / 100, DAM.lon, T0, k, k);
    }
    const dBefore = slot("d");
    store.retainCoverage([]);
    expect(store.count).toBe(0);
    for (const [k, id] of ["a", "b", "c", "d"].entries()) {
      store.upsert(id, DAM.lat + k / 100, DAM.lon, T0, k, k);
    }
    store.sweepStale(T0 + 1, 0);
    expect(store.count).toBe(0);
    for (const [k, id] of ["a", "b", "c", "d"].entries()) {
      store.upsert(id, k === 1 ? 48.85 : DAM.lat + k / 100, DAM.lon, T0, k, k);
    }
    store.retainCoverage([quadkeyFor(DAM.lon, DAM.lat, 6)]);
    expect(store.ids).toEqual(["a", "d", "c"]);
    expect(store.indexOf("d")).toBe(1);
    expect(slot("d")).toEqual(dBefore);
    expect(store.viewOf("d")?.speedMps).toBe(3);
  });
});

describe("zones", () => {
  const violet = packRgba(hexToRgba("#6d5dfc"));
  const amber = packRgba(hexToRgba("#f59f00"));

  it("tints devices inside an active zone with its colour; the smallest zone wins", () => {
    const { store, words } = setup();
    store.setZones([
      { id: "big", ...DAM, radiusM: 5_000, color: amber, active: true },
      { id: "small", ...DAM, radiusM: 300, color: violet, active: true },
      { id: "off", ...DAM, radiusM: 100, color: 1, active: false },
    ]);
    const near = destination(DAM, 45, 150);
    const mid = destination(DAM, 45, 2_000);
    const out = destination(DAM, 45, 6_000);
    store.upsert("near", near.lat, near.lon, T0, 1, 1);
    store.upsert("mid", mid.lat, mid.lon, T0, 1, 1);
    store.upsert("out", out.lat, out.lon, T0, 1, 1);
    expect(words("near")).toBe(violet);
    expect(words("mid")).toBe(amber);
    expect(words("out")).toBe(0);
    expect(store.viewOf("near")?.zoneId).toBe("small");
    expect(store.inZone("big")).toEqual(["mid"]);
    expect((store.instances[(store.indexOf("mid") as number) * STRIDE + 11] ?? 0) & 2).toBe(
      Flag.InZone,
    );
  });

  it("re-tints the fleet when zones change", () => {
    const { store, words } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 1, 1);
    store.takeDirty();
    store.setZones([{ id: "z", ...DAM, radiusM: 50, color: violet, active: true }]);
    expect(words("a")).toBe(violet);
    expect(store.takeDirty()).toEqual([0, 0]);
    store.setZones([]);
    expect(words("a")).toBe(0);
    expect(store.viewOf("a")?.zoneId).toBeNull();
  });
});

describe("rendering support", () => {
  it("rebases offsets on a new origin", () => {
    const { store, slot } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 1, 1);
    const version = store.layoutVersion;
    store.rebase(mercatorX(DAM.lon), mercatorY(DAM.lat));
    expect(slot("a")[InstanceLayout.to]).toBeCloseTo(0, 12);
    expect(store.layoutVersion).toBe(version + 1);
  });

  it("counts devices inside a box, across the antimeridian too", () => {
    const { store } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 5, 1);
    store.upsert("b", DAM.lat, DAM.lon + 0.5, T0, 0, 1);
    store.upsert("c", 10, 179.9, T0, 5, 1);
    expect(store.countWithin(4.8, 52.3, 5.0, 52.4)).toEqual({ total: 1, moving: 1 });
    expect(store.countWithin(4.8, 52.3, 5.5, 52.4)).toEqual({ total: 2, moving: 1 });
    // An unwrapped view east of the antimeridian sees the device at 179.9° as -180.1°.
    expect(store.countWithin(-180.5, 9, -179.9, 11)).toEqual({ total: 1, moving: 1 });
    expect(store.countWithin(-200, -90, 200, 90).total).toBe(3);
  });

  it("picks the nearest device within a radius", () => {
    const { store, clock } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 1, 1);
    store.upsert("b", DAM.lat + 0.001, DAM.lon, T0, 1, 1);
    const x = mercatorX(DAM.lon);
    const y = mercatorY(DAM.lat + 0.0009);
    expect(store.pick(x, y, 1e-5, clock.t)).toBe(store.indexOf("b"));
    expect(store.pick(x, y, 1e-9, clock.t)).toBeNull();
  });

  it("picks a device from any world copy of the map", () => {
    const { store, clock } = setup();
    store.upsert("a", DAM.lat, DAM.lon, T0, 1, 1);
    const x = mercatorX(DAM.lon);
    const y = mercatorY(DAM.lat);
    for (const turns of [1, 2, -1, -3]) {
      expect(store.pick(x + turns, y, 1e-6, clock.t)).toBe(store.indexOf("a"));
    }
  });

  it("keeps a device across the antimeridian next to the origin, not a world away", () => {
    const { store, slot } = setup();
    store.upsert("east", 0, -179.99, T0, 1, 90);
    store.rebase(mercatorX(179.99), mercatorY(0));
    const offset = slot("east")[InstanceLayout.to] as number;
    expect(offset).toBeGreaterThan(0);
    expect(offset).toBeCloseTo(0.02 / 360, 9);
    // And every offset stays within half a world of the origin.
    store.upsert("far", 0, 0, T0, 1, 90);
    expect(Math.abs(slot("far")[InstanceLayout.to] as number)).toBeLessThanOrEqual(0.5);
  });

  it("glides across the antimeridian the short way", () => {
    const { store, clock, slot } = setup();
    store.upsert("ship", -16.5, 179.9995, T0, 8, 90);
    clock.t += 1;
    store.upsert("ship", -16.5, -179.9995, T0 + 1_000, 8, 90);
    const [from, to] = [
      slot("ship")[InstanceLayout.from] as number,
      slot("ship")[InstanceLayout.to] as number,
    ];
    // About 107 m apart, drawn as a glide rather than a jump or a sweep across the world
    // (float32 offsets half a world from the default origin: ~3e-8 precision).
    expect(slot("ship")[InstanceLayout.time + 1]).toBeGreaterThan(0);
    expect(Math.abs(to - from)).toBeCloseTo(0.001 / 360, 7);
    const [halfway] = store.positionAt(store.indexOf("ship") as number, clock.t + 0.4);
    expect(Math.abs(halfway - Math.round(halfway))).toBeLessThan(0.001 / 360);
  });

  it("reports and clears the dirty range", () => {
    const { store } = setup();
    expect(store.takeDirty()).toBeNull();
    store.upsert("a", DAM.lat, DAM.lon, T0, 1, 1);
    store.upsert("b", DAM.lat, DAM.lon, T0, 1, 1);
    expect(store.takeDirty()).toEqual([0, 1]);
    expect(store.takeDirty()).toBeNull();
    store.upsert("b", DAM.lat, DAM.lon, T0 + 1, 1, 1);
    expect(store.takeDirty()).toEqual([1, 1]);
  });
});
