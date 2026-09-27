/**
 * Geodesy on the WGS84 ellipsoid, for display.
 *
 * The server decides containment with PostGIS `ST_DWithin` on `geography`, which is exact on the
 * spheroid. The dashboard draws zones and tints devices with the same model so that what the user
 * sees agrees with what the engine decides: a spherical approximation would misplace the edge of a
 * 100 km zone by up to ~300 m. Vincenty's formulae are accurate to well under a millimetre at these
 * distances (verified against GeographicLib in the tests).
 */

const A = 6378137;
const F = 1 / 298.257223563;
const B = A * (1 - F);
const DEG = Math.PI / 180;
const MAX_ITERATIONS = 200;
const EPSILON = 1e-12;
/** Meridian arc per degree of latitude is smallest at the equator: a(1 − e²)·π/180. */
const MIN_METERS_PER_DEGREE_LAT = (A * (1 - F * (2 - F)) * Math.PI) / 180;

export interface LatLon {
  lat: number;
  lon: number;
}

/** Destination of the geodesic of length `distanceM` leaving `from` at `azimuthDeg` (Vincenty). */
export function destination(from: LatLon, azimuthDeg: number, distanceM: number): LatLon {
  const alpha1 = azimuthDeg * DEG;
  const sinAlpha1 = Math.sin(alpha1);
  const cosAlpha1 = Math.cos(alpha1);
  const tanU1 = (1 - F) * Math.tan(from.lat * DEG);
  const cosU1 = 1 / Math.sqrt(1 + tanU1 * tanU1);
  const sinU1 = tanU1 * cosU1;
  const sigma1 = Math.atan2(tanU1, cosAlpha1);
  const sinAlpha = cosU1 * sinAlpha1;
  const cosSqAlpha = 1 - sinAlpha * sinAlpha;
  const uSq = (cosSqAlpha * (A * A - B * B)) / (B * B);
  const bigA = 1 + (uSq / 16384) * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)));
  const bigB = (uSq / 1024) * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)));

  let sigma = distanceM / (B * bigA);
  let sinSigma = 0;
  let cosSigma = 1;
  let cos2SigmaM = 1;
  for (let i = 0; i < MAX_ITERATIONS; i++) {
    cos2SigmaM = Math.cos(2 * sigma1 + sigma);
    sinSigma = Math.sin(sigma);
    cosSigma = Math.cos(sigma);
    const deltaSigma =
      bigB *
      sinSigma *
      (cos2SigmaM +
        (bigB / 4) *
          (cosSigma * (-1 + 2 * cos2SigmaM * cos2SigmaM) -
            (bigB / 6) *
              cos2SigmaM *
              (-3 + 4 * sinSigma * sinSigma) *
              (-3 + 4 * cos2SigmaM * cos2SigmaM)));
    const next = distanceM / (B * bigA) + deltaSigma;
    const converged = Math.abs(next - sigma) < EPSILON;
    sigma = next;
    if (converged) break;
  }
  sinSigma = Math.sin(sigma);
  cosSigma = Math.cos(sigma);
  cos2SigmaM = Math.cos(2 * sigma1 + sigma);

  const x = sinU1 * sinSigma - cosU1 * cosSigma * cosAlpha1;
  const lat = Math.atan2(
    sinU1 * cosSigma + cosU1 * sinSigma * cosAlpha1,
    (1 - F) * Math.sqrt(sinAlpha * sinAlpha + x * x),
  );
  const lambda = Math.atan2(sinSigma * sinAlpha1, cosU1 * cosSigma - sinU1 * sinSigma * cosAlpha1);
  const c = (F / 16) * cosSqAlpha * (4 + F * (4 - 3 * cosSqAlpha));
  const l =
    lambda -
    (1 - c) *
      F *
      sinAlpha *
      (sigma + c * sinSigma * (cos2SigmaM + c * cosSigma * (-1 + 2 * cos2SigmaM * cos2SigmaM)));
  return { lat: lat / DEG, lon: normalizeLon(from.lon + l / DEG) };
}

export interface Inverse {
  distanceM: number;
  /** Initial azimuth at `from`, degrees clockwise from north in [0, 360). */
  azimuthDeg: number;
}

/**
 * Geodesic distance and initial azimuth between two points (Vincenty). For nearly antipodal
 * points, where the iteration may not converge, it falls back to the spherical solution — those
 * distances never occur in this application (zones are at most 100 km across).
 */
export function inverse(from: LatLon, to: LatLon): Inverse {
  const l = normalizeLon(to.lon - from.lon) * DEG;
  const tanU1 = (1 - F) * Math.tan(from.lat * DEG);
  const cosU1 = 1 / Math.sqrt(1 + tanU1 * tanU1);
  const sinU1 = tanU1 * cosU1;
  const tanU2 = (1 - F) * Math.tan(to.lat * DEG);
  const cosU2 = 1 / Math.sqrt(1 + tanU2 * tanU2);
  const sinU2 = tanU2 * cosU2;

  let lambda = l;
  let sinLambda = 0;
  let cosLambda = 1;
  let sinSigma = 0;
  let cosSigma = 1;
  let sigma = 0;
  let cosSqAlpha = 1;
  let cos2SigmaM = 0;
  let converged = false;
  for (let i = 0; i < MAX_ITERATIONS; i++) {
    sinLambda = Math.sin(lambda);
    cosLambda = Math.cos(lambda);
    const t1 = cosU2 * sinLambda;
    const t2 = cosU1 * sinU2 - sinU1 * cosU2 * cosLambda;
    sinSigma = Math.sqrt(t1 * t1 + t2 * t2);
    if (sinSigma === 0) {
      return { distanceM: 0, azimuthDeg: 0 };
    }
    cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLambda;
    sigma = Math.atan2(sinSigma, cosSigma);
    const sinAlpha = (cosU1 * cosU2 * sinLambda) / sinSigma;
    cosSqAlpha = 1 - sinAlpha * sinAlpha;
    cos2SigmaM = cosSqAlpha !== 0 ? cosSigma - (2 * sinU1 * sinU2) / cosSqAlpha : 0;
    const c = (F / 16) * cosSqAlpha * (4 + F * (4 - 3 * cosSqAlpha));
    const previous = lambda;
    lambda =
      l +
      (1 - c) *
        F *
        sinAlpha *
        (sigma + c * sinSigma * (cos2SigmaM + c * cosSigma * (-1 + 2 * cos2SigmaM * cos2SigmaM)));
    if (Math.abs(lambda - previous) < EPSILON) {
      converged = true;
      break;
    }
  }
  if (!converged) {
    return sphericalInverse(from, to);
  }
  const uSq = (cosSqAlpha * (A * A - B * B)) / (B * B);
  const bigA = 1 + (uSq / 16384) * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)));
  const bigB = (uSq / 1024) * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)));
  const deltaSigma =
    bigB *
    sinSigma *
    (cos2SigmaM +
      (bigB / 4) *
        (cosSigma * (-1 + 2 * cos2SigmaM * cos2SigmaM) -
          (bigB / 6) *
            cos2SigmaM *
            (-3 + 4 * sinSigma * sinSigma) *
            (-3 + 4 * cos2SigmaM * cos2SigmaM)));
  const distanceM = B * bigA * (sigma - deltaSigma);
  const azimuth = Math.atan2(cosU2 * sinLambda, cosU1 * sinU2 - sinU1 * cosU2 * cosLambda);
  return { distanceM, azimuthDeg: (azimuth / DEG + 360) % 360 };
}

const MEAN_RADIUS = (2 * A + B) / 3;

function sphericalInverse(from: LatLon, to: LatLon): Inverse {
  const phi1 = from.lat * DEG;
  const phi2 = to.lat * DEG;
  const dPhi = phi2 - phi1;
  const dLambda = normalizeLon(to.lon - from.lon) * DEG;
  const h = Math.sin(dPhi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
  const distanceM = 2 * MEAN_RADIUS * Math.asin(Math.min(1, Math.sqrt(h)));
  const y = Math.sin(dLambda) * Math.cos(phi2);
  const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLambda);
  return { distanceM, azimuthDeg: (((Math.atan2(y, x) / DEG) % 360) + 360) % 360 };
}

export function distance(from: LatLon, to: LatLon): number {
  return inverse(from, to).distanceM;
}

/** Longitude brought into [-180, 180). */
export function normalizeLon(lon: number): number {
  if (lon >= -180 && lon < 180) return lon;
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/**
 * Ring of a geodesic circle as GeoJSON positions `[lon, lat]`, closed, counter-clockwise.
 * Longitudes are unwrapped around the centre (they may leave [-180, 180]) so a circle crossing
 * the antimeridian stays one continuous polygon for the renderer.
 */
export function circleRing(center: LatLon, radiusM: number, segments = 128): [number, number][] {
  const ring: [number, number][] = [];
  for (let i = 0; i <= segments; i++) {
    const azimuth = 360 - (360 * (i % segments)) / segments;
    const p = destination(center, azimuth % 360, radiusM);
    let lon = p.lon;
    while (lon - center.lon > 180) lon -= 360;
    while (lon - center.lon < -180) lon += 360;
    ring.push([lon, p.lat]);
  }
  return ring;
}

/** Segment count that keeps the polygon edge within a fraction of a pixel of the true circle. */
export function segmentsFor(radiusM: number): number {
  if (radiusM < 200) return 64;
  if (radiusM < 5_000) return 128;
  return 192;
}

export interface Envelope {
  south: number;
  north: number;
  /** West/east edges; `null` when the envelope spans every longitude. May cross ±180. */
  west: number | null;
  east: number | null;
}

/**
 * Conservative lon/lat box around a geodesic circle — the same bound the database uses as its
 * spatial prefilter. Every point within `radiusM` of `center` is inside it.
 */
export function envelope(center: LatLon, radiusM: number): Envelope {
  const dLat = radiusM / MIN_METERS_PER_DEGREE_LAT + 1e-9;
  const south = Math.max(center.lat - dLat, -90);
  const north = Math.min(center.lat + dLat, 90);
  const widest = Math.max(Math.abs(south), Math.abs(north));
  if (widest >= 89.999999) {
    return { south, north, west: null, east: null };
  }
  const dLon = radiusM / (A * Math.cos(widest * DEG) * DEG) + 1e-9;
  if (dLon >= 180) {
    return { south, north, west: null, east: null };
  }
  return { south, north, west: center.lon - dLon, east: center.lon + dLon };
}

export function envelopeContains(box: Envelope, point: LatLon): boolean {
  if (point.lat < box.south || point.lat > box.north) return false;
  if (box.west === null || box.east === null) return true;
  const lon = normalizeLon(point.lon);
  const west = normalizeLon(box.west);
  const east = normalizeLon(box.east);
  return west <= east ? lon >= west && lon <= east : lon >= west || lon <= east;
}

/** Is `point` within `radiusM` of `center` on the ellipsoid? */
export function withinRadius(center: LatLon, radiusM: number, point: LatLon, box?: Envelope) {
  if (!envelopeContains(box ?? envelope(center, radiusM), point)) return false;
  return inverse(center, point).distanceM <= radiusM;
}

/** Spherical-Mercator x in [0, 1] (the unit MapLibre's custom layers render in). */
export function mercatorX(lon: number): number {
  return (180 + lon) / 360;
}

/** Spherical-Mercator y in [0, 1], 0 at the north edge of the projection. */
export function mercatorY(lat: number): number {
  const clamped = Math.max(-85.05112877980659, Math.min(85.05112877980659, lat));
  return (180 - (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + (clamped * DEG) / 2))) / 360;
}

export function lonFromMercatorX(x: number): number {
  return x * 360 - 180;
}

export function latFromMercatorY(y: number): number {
  const y2 = 180 - y * 360;
  return (360 / Math.PI) * Math.atan(Math.exp((y2 * Math.PI) / 180)) - 90;
}

/** Metres per Mercator unit at latitude `lat` (the projection's scale factor). */
export function metersPerMercatorUnit(lat: number): number {
  return 2 * Math.PI * A * Math.cos(lat * DEG);
}
