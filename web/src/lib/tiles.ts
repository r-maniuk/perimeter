/**
 * Web-Mercator tiles and quadkeys, mirroring `perimeter/domain/tiles.py`.
 *
 * The live channel subscribes to position subjects by quadkey prefix: the server answers a
 * `viewport` message with the prefixes that cover it and streams every leaf tile below them. The
 * dashboard computes the same cover so it knows which area its device state is authoritative for,
 * and drops devices that fell outside it (no further updates would ever reach them).
 */

export const MAX_MERCATOR_LAT = 85.05112877980659;

export interface Tile {
  x: number;
  y: number;
  z: number;
}

export interface BBox {
  west: number;
  south: number;
  east: number;
  north: number;
}

const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(value, high));

export function tileFor(lon: number, lat: number, zoom: number): Tile {
  const n = 2 ** zoom;
  const latR = (clamp(lat, -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT) * Math.PI) / 180;
  const x = Math.trunc(((clamp(lon, -180, 180) + 180) / 360) * n);
  const y = Math.trunc(((1 - Math.asinh(Math.tan(latR)) / Math.PI) / 2) * n);
  return { x: clamp(x, 0, n - 1), y: clamp(y, 0, n - 1), z: zoom };
}

export function quadkey(tile: Tile): string {
  let key = "";
  for (let level = tile.z; level > 0; level--) {
    const mask = 2 ** (level - 1);
    const digit = (Math.floor(tile.x / mask) % 2) + 2 * (Math.floor(tile.y / mask) % 2);
    key += String(digit);
  }
  return key;
}

export function tileOfQuadkey(key: string): Tile {
  let x = 0;
  let y = 0;
  for (const digit of key) {
    if (digit !== "0" && digit !== "1" && digit !== "2" && digit !== "3") {
      throw new Error(`invalid quadkey digit ${JSON.stringify(digit)} in ${JSON.stringify(key)}`);
    }
    const value = Number(digit);
    x = x * 2 + (value & 1);
    y = y * 2 + (value >> 1);
  }
  return { x, y, z: key.length };
}

export function quadkeyFor(lon: number, lat: number, zoom: number): string {
  return quadkey(tileFor(lon, lat, zoom));
}

export function tileBounds(tile: Tile): BBox {
  const n = 2 ** tile.z;
  const latOf = (y: number) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
  return {
    west: (tile.x / n) * 360 - 180,
    south: latOf(tile.y + 1),
    east: ((tile.x + 1) / n) * 360 - 180,
    north: latOf(tile.y),
  };
}

/** Bring a longitude into [-180, 180] without moving values already inside it. */
function wrap(lon: number): number {
  if (lon >= -180 && lon <= 180) return lon;
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/**
 * A box with its longitudes folded into [-180, 180] — the form the live protocol takes. A map
 * panned around the world reports unwrapped longitudes (and the server refuses anything past
 * ±540°); folded, a box crossing the antimeridian has `west > east`, and one spanning 360° or more
 * is the whole world. Folding never changes which tiles cover the box.
 */
export function foldBBox(bbox: BBox): BBox {
  if (bbox.east - bbox.west >= 360) {
    return { west: -180, south: bbox.south, east: 180, north: bbox.north };
  }
  return { west: wrap(bbox.west), south: bbox.south, east: wrap(bbox.east), north: bbox.north };
}

function splitAntimeridian(bbox: BBox): BBox[] {
  const south = clamp(Math.min(bbox.south, bbox.north), -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT);
  const north = clamp(Math.max(bbox.south, bbox.north), -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT);
  if (bbox.east - bbox.west >= 360) {
    return [{ west: -180, south, east: 180, north }];
  }
  const west = wrap(bbox.west);
  const east = wrap(bbox.east);
  if (west <= east) {
    return [{ west, south, east, north }];
  }
  return [
    { west, south, east: 180, north },
    { west: -180, south, east, north },
  ];
}

/**
 * Quadkeys of the deepest level at which at most `maxTiles` tiles cover `bbox` — the same prefixes
 * the server subscribes a session to. Sorted, duplicate-free; `[""]` means the whole world.
 */
export function coveringQuadkeys(bbox: BBox, maxTiles: number, maxZoom: number): string[] {
  if (maxTiles < 1) {
    throw new Error("maxTiles must be at least 1");
  }
  const parts = splitAntimeridian(bbox);
  for (let zoom = maxZoom; zoom >= 0; zoom--) {
    const cells = new Set<number>();
    const keys: string[] = [];
    let tooMany = false;
    for (const part of parts) {
      const topLeft = tileFor(part.west, part.north, zoom);
      const bottomRight = tileFor(part.east, part.south, zoom);
      const width = bottomRight.x - topLeft.x + 1;
      const height = bottomRight.y - topLeft.y + 1;
      if (width * height > maxTiles) {
        tooMany = true;
        break;
      }
      for (let x = topLeft.x; x <= bottomRight.x; x++) {
        for (let y = topLeft.y; y <= bottomRight.y; y++) {
          const id = x * 2 ** zoom + y;
          if (!cells.has(id)) {
            cells.add(id);
            keys.push(quadkey({ x, y, z: zoom }));
          }
        }
      }
      if (cells.size > maxTiles) {
        tooMany = true;
        break;
      }
    }
    if (!tooMany) {
      return keys.sort();
    }
  }
  return [""];
}

/**
 * The tiles at `zoom` under `bbox` as a comparable key: one range of columns and rows per side of
 * the antimeridian. The ranges at every coarser zoom follow from these, so two boxes with the same
 * span get the same cover from {@link coveringQuadkeys} with `maxZoom = zoom`, whatever the tile
 * budget — a viewport only needs to reach the server again when its span changes.
 */
export function tileSpan(bbox: BBox, zoom: number): string {
  return splitAntimeridian(bbox)
    .map((part) => {
      const topLeft = tileFor(part.west, part.north, zoom);
      const bottomRight = tileFor(part.east, part.south, zoom);
      return `${topLeft.x}-${bottomRight.x}/${topLeft.y}-${bottomRight.y}`;
    })
    .join(" ");
}

/**
 * Membership test against a set of prefixes: is the leaf tile `key` at or below one of them?
 * Prefix sets are small (≤ the server's viewport tile budget), so a linear scan is fastest.
 */
export function coveredBy(key: string, prefixes: readonly string[]): boolean {
  for (const prefix of prefixes) {
    if (key.startsWith(prefix)) return true;
  }
  return false;
}
