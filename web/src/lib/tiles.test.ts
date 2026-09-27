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
