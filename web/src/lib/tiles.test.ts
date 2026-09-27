import { describe, expect, it } from "vitest";
import vectors from "./__fixtures__/tiles.json";
import {
  coveredBy,
  coveringQuadkeys,
  foldBBox,
  quadkey,
  quadkeyFor,
  tileBounds,
  tileFor,
  tileOfQuadkey,
  tileSpan,
} from "./tiles";

/**
 * `__fixtures__/tiles.json` holds outputs of the server's own implementation
 * (`perimeter.domain.tiles`) for fixed and seeded-random inputs, including antimeridian-crossing,
 * inverted, polar and whole-world boxes. Agreement here means the dashboard reasons about exactly
 * the prefixes the API subscribes it to.
 */
describe("tiles agree with the server implementation", () => {
  it.each(vectors.tiles)("tile of ($lon, $lat) at z$z", (v) => {
    const tile = tileFor(v.lon, v.lat, v.z);
    expect(tile).toEqual({ x: v.x, y: v.y, z: v.z });
    expect(quadkey(tile)).toBe(v.key);
    expect(tileOfQuadkey(v.key)).toEqual(tile);
    const bounds = tileBounds(tile);
    const [west, south, east, north] = v.bounds as [number, number, number, number];
    expect(bounds.west).toBeCloseTo(west, 9);
    expect(bounds.south).toBeCloseTo(south, 9);
    expect(bounds.east).toBeCloseTo(east, 9);
    expect(bounds.north).toBeCloseTo(north, 9);
  });

  it.each(vectors.covers)("cover of $bbox with ≤$maxTiles tiles up to z$maxZoom", (v) => {
    const [west, south, east, north] = v.bbox as [number, number, number, number];
    expect(coveringQuadkeys({ west, south, east, north }, v.maxTiles, v.maxZoom)).toEqual(v.keys);
  });
});

describe("quadkeys", () => {
  it("round-trips every tile of a small zoom level", () => {
    for (let x = 0; x < 8; x++) {
      for (let y = 0; y < 8; y++) {
        expect(tileOfQuadkey(quadkey({ x, y, z: 3 }))).toEqual({ x, y, z: 3 });
      }
    }
  });

  it("rejects digits outside 0-3", () => {
    expect(() => tileOfQuadkey("0124")).toThrow(/invalid quadkey digit/);
  });

  it("nests: a point's key at a deeper zoom extends its key at a shallower zoom", () => {
    const deep = quadkeyFor(4.9041, 52.3676, 12);
    for (let z = 0; z <= 12; z++) {
      expect(deep.startsWith(quadkeyFor(4.9041, 52.3676, z))).toBe(true);
    }
  });

  it("answers prefix membership", () => {
    expect(coveredBy("120202110112", ["1202021101", "3"])).toBe(true);
    expect(coveredBy("120202110112", ["1202021102"])).toBe(false);
    expect(coveredBy("120202110112", [""])).toBe(true);
    expect(coveredBy("0", [])).toBe(false);
  });

  it("refuses an empty tile budget", () => {
    expect(() => coveringQuadkeys({ west: 0, south: 0, east: 1, north: 1 }, 0, 12)).toThrow();
  });
});

describe("tile spans", () => {
  const amsterdam = { west: 4.79, south: 52.33, east: 4.99, north: 52.41 };

  /** Deterministic pseudo-random numbers (mulberry32), so failures reproduce. */
  function seeded(seed: number): () => number {
    let a = seed;
    return () => {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296;
    };
  }

  it("stays the same while a view moves inside its leaf tiles, and changes when it leaves them", () => {
    const nudged = { ...amsterdam, west: amsterdam.west + 1e-4, east: amsterdam.east + 1e-4 };
    expect(tileSpan(nudged, 12)).toBe(tileSpan(amsterdam, 12));
    const moved = { ...amsterdam, west: amsterdam.west + 0.1, east: amsterdam.east + 0.1 };
    expect(tileSpan(moved, 12)).not.toBe(tileSpan(amsterdam, 12));
  });

  it("describes both sides of a view across the antimeridian, however it is unwrapped", () => {
    const across = { west: 170, south: -20, east: 190, north: -10 };
    expect(tileSpan(across, 4).split(" ")).toHaveLength(2);
    expect(tileSpan(foldBBox(across), 4)).toBe(tileSpan(across, 4));
    expect(tileSpan({ west: 530, south: -20, east: 550, north: -10 }, 4)).toBe(tileSpan(across, 4));
  });

  it("decides the server's cover for every tile budget", () => {
    const random = seeded(7);
    let equalSpans = 0;
    for (let i = 0; i < 400; i++) {
      // Views of many sizes, and a second view panned or zoomed a little from each.
      const size = 10 ** (-3 + random() * 4.5);
      const west = -180 + random() * 360;
      const south = -70 + random() * 140;
      const a = { west, south, east: west + size * 1.6, north: Math.min(85, south + size) };
      const shift = size * (random() - 0.5) * 0.2;
      const grow = 1 + (random() - 0.5) * 0.1;
      const b = {
        west: a.west + shift,
        south: a.south + shift / 2,
        east: a.west + shift + (a.east - a.west) * grow,
        north: Math.min(85, a.south + shift / 2 + (a.north - a.south) * grow),
      };
      if (tileSpan(a, 12) !== tileSpan(b, 12)) continue;
      equalSpans++;
      for (const budget of [1, 4, 16, 64, 256]) {
        expect(coveringQuadkeys(b, budget, 12)).toEqual(coveringQuadkeys(a, budget, 12));
      }
    }
    // The comparison must actually have been exercised.
    expect(equalSpans).toBeGreaterThan(50);
  });
});

describe("folding a panned viewport", () => {
  const amsterdam = { west: 4.79, south: 52.33, east: 4.99, north: 52.41 };
  const turns = (box: typeof amsterdam, k: number) => ({
    ...box,
    west: box.west + 360 * k,
    east: box.east + 360 * k,
  });

  it("leaves a box inside the first world untouched", () => {
    expect(foldBBox(amsterdam)).toEqual(amsterdam);
    expect(foldBBox({ west: -180, south: -10, east: 180, north: 10 })).toEqual({
      west: -180,
      south: -10,
      east: 180,
      north: 10,
    });
  });

  it.each([1, 2, -1, -3])("brings a box %i world(s) away back to the first world", (k) => {
    const folded = foldBBox(turns(amsterdam, k));
    expect(folded.west).toBeCloseTo(amsterdam.west, 9);
    expect(folded.east).toBeCloseTo(amsterdam.east, 9);
  });

  it("keeps a box across the antimeridian as west > east", () => {
    expect(foldBBox({ west: 170, south: -20, east: 190, north: -10 })).toEqual({
      west: 170,
      south: -20,
      east: -170,
      north: -10,
    });
    expect(foldBBox({ west: 530, south: 0, east: 550, north: 1 })).toMatchObject({
      west: 170,
      east: -170,
    });
  });

  it("turns a view of the whole world, however panned, into the whole world", () => {
    expect(foldBBox({ west: 250, south: -80, east: 650, north: 80 })).toEqual({
      west: -180,
      south: -80,
      east: 180,
      north: 80,
    });
  });

  it("covers the same tiles as the unfolded box", () => {
    const boxes = [
      turns(amsterdam, 1),
      turns(amsterdam, -2),
      { west: 170, south: -20, east: 190, north: -10 },
      { west: 530, south: 0, east: 550, north: 1 },
      { west: -200, south: 60, east: -170, north: 70 },
      { west: 250, south: -80, east: 650, north: 80 },
    ];
    for (const box of boxes) {
      expect(coveringQuadkeys(foldBBox(box), 16, 12)).toEqual(coveringQuadkeys(box, 16, 12));
      const folded = foldBBox(box);
      expect(Math.abs(folded.west)).toBeLessThanOrEqual(180);
      expect(Math.abs(folded.east)).toBeLessThanOrEqual(180);
    }
  });
});
