import { describe, expect, it } from "vitest";
import reference from "./__fixtures__/geodesics.json";
import {
  circleRing,
  destination,
  distance,
  envelope,
  envelopeContains,
  inverse,
  latFromMercatorY,
  lonFromMercatorX,
  mercatorX,
  mercatorY,
  normalizeLon,
  segmentsFor,
  withinRadius,
} from "./geodesy";

/** Longitude difference folded into [-180, 180]. */
const dLon = (a: number, b: number) => ((((a - b + 180) % 360) + 360) % 360) - 180;

/**
 * `__fixtures__/geodesics.json` was produced with GeographicLib 2.1 (Karney's algorithms,
 * accurate to nanometres), the same reference the server's spatial tests use.
 */
describe("agrees with GeographicLib on WGS84", () => {
  it.each(reference.direct)("destination from ($lat, $lon) at $azi° for $s m", (c) => {
    const p = destination({ lat: c.lat, lon: c.lon }, c.azi, c.s);
    expect(Math.abs(p.lat - c.lat2)).toBeLessThan(1e-9);
    expect(Math.abs(dLon(p.lon, c.lon2))).toBeLessThan(1e-9);
  });

  it.each(reference.inverse)("distance ($lat1, $lon1) → ($lat2, $lon2)", (c) => {
    const r = inverse({ lat: c.lat1, lon: c.lon1 }, { lat: c.lat2, lon: c.lon2 });
    expect(Math.abs(r.distanceM - c.s12)).toBeLessThan(1e-4);
    if (c.s12 > 1) {
      // Azimuth error expressed as the sideways miss at the far end: under 0.1 mm.
      const azimuth = ((c.azi1 % 360) + 360) % 360;
      const missM = (Math.abs(dLon(r.azimuthDeg, azimuth)) * Math.PI * c.s12) / 180;
      expect(missM).toBeLessThan(1e-4);
    }
  });
});

describe("circles", () => {
  const amsterdam = { lat: 52.3676, lon: 4.9041 };

  it("puts every vertex exactly one radius from the centre", () => {
    for (const radius of [10, 850, 25_000, 100_000]) {
      const ring = circleRing(amsterdam, radius, segmentsFor(radius));
      for (const [lon, lat] of ring) {
        expect(Math.abs(distance(amsterdam, { lat, lon }) - radius)).toBeLessThan(1e-5);
      }
    }
  });

  it("is closed and counter-clockwise, as GeoJSON exterior rings must be", () => {
    const ring = circleRing(amsterdam, 1_000, 64);
    expect(ring).toHaveLength(65);
    expect(ring[0]).toEqual(ring[64]);
    let area2 = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      const [x1, y1] = ring[i] as [number, number];
      const [x2, y2] = ring[i + 1] as [number, number];
      area2 += x1 * y2 - x2 * y1;
    }
    expect(area2).toBeGreaterThan(0);
  });

  it("stays continuous across the antimeridian", () => {
    const ring = circleRing({ lat: 10, lon: 179.9 }, 50_000, 64);
    const lons = ring.map(([lon]) => lon);
    expect(Math.max(...lons)).toBeGreaterThan(180);
    for (let i = 1; i < lons.length; i++) {
      expect(Math.abs((lons[i] as number) - (lons[i - 1] as number))).toBeLessThan(1);
    }
  });
});

describe("envelope prefilter", () => {
  it("never excludes a point inside the circle", () => {
    const centres = [
      { lat: 52.3676, lon: 4.9041 },
      { lat: -70, lon: 179.99 },
      { lat: 0, lon: -180 },
      { lat: 84, lon: 30 },
    ];
    for (const centre of centres) {
      for (const radius of [10, 1_000, 100_000]) {
        const box = envelope(centre, radius);
        for (let azimuth = 0; azimuth < 360; azimuth += 7.5) {
          const edge = destination(centre, azimuth, radius * (1 - 1e-9));
          expect(envelopeContains(box, edge)).toBe(true);
          expect(withinRadius(centre, radius, edge, box)).toBe(true);
        }
      }
    }
  });

  it("rejects distant points cheaply and decides the rim exactly", () => {
    const centre = { lat: 52.3676, lon: 4.9041 };
    expect(withinRadius(centre, 500, { lat: 52.5, lon: 4.9 })).toBe(false);
    const outside = destination(centre, 42, 500.01);
    const inside = destination(centre, 42, 499.99);
    expect(withinRadius(centre, 500, outside)).toBe(false);
    expect(withinRadius(centre, 500, inside)).toBe(true);
  });

  it("spans every longitude near the poles", () => {
    const box = envelope({ lat: 89.95, lon: 0 }, 50_000);
    expect(box.west).toBeNull();
    expect(envelopeContains(box, { lat: 89.9, lon: 179 })).toBe(true);
  });
});

describe("mercator", () => {
  it("round-trips coordinates", () => {
    for (const [lon, lat] of [
      [4.9041, 52.3676],
      [-179.5, -60],
      [0, 0],
      [120.25, 84],
    ] as const) {
      expect(lonFromMercatorX(mercatorX(lon))).toBeCloseTo(lon, 10);
      expect(latFromMercatorY(mercatorY(lat))).toBeCloseTo(lat, 10);
    }
    expect(mercatorY(0)).toBeCloseTo(0.5, 12);
    expect(mercatorX(-180)).toBe(0);
  });

  it("normalises longitudes", () => {
    expect(normalizeLon(181)).toBeCloseTo(-179, 12);
    expect(normalizeLon(-540)).toBeCloseTo(-180, 12);
    expect(normalizeLon(179.5)).toBe(179.5);
  });
});
