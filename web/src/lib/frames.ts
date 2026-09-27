/**
 * Binary position frames, as produced by the engine and forwarded untouched by every API replica.
 *
 * Positions are the only high-volume stream the dashboard receives, so they arrive as compact
 * little-endian frames rather than JSON. Every array in a frame starts on a 4-byte boundary and
 * every frame length is a multiple of 4, which lets the decoder expose typed-array *views* over the
 * received buffer instead of copying: decoding a bundle of 10,000 devices allocates a handful of
 * view objects and one string per device id.
 *
 * Tile frame::
 *
 *     0   u8   magic 0xB7
 *     1   u8   version (1)
 *     2   u8   kind (1 = live update, 2 = snapshot)
 *     3   u8   zoom
 *     4   u32  tile x
 *     8   u32  tile y
 *     12  u64  base time, epoch milliseconds (smallest timestamp in the frame)
 *     20  u32  count N
 *     24  i32[N]  latitude  x 1e7
 *         i32[N]  longitude x 1e7
 *         u32[N]  recorded_at - base time, milliseconds
 *         u16[N]  speed, cm/s (0xFFFF = unknown)
 *         u16[N]  heading, centidegrees (0xFFFF = unknown)
 *         u32     length L of the id blob
 *         u8[L]   device ids, UTF-8, separated by 0x00
 *         (zero padding to a multiple of 4)
 *
 * Bundle (one WebSocket binary message)::
 *
 *     0   u8   magic 0xB8
 *     1   u8   version (1)
 *     2   u16  count K
 *     then K times: u32 length, frame bytes
 */

export const FRAME_MAGIC = 0xb7;
export const BUNDLE_MAGIC = 0xb8;
export const FRAME_VERSION = 1;
export const UNKNOWN_U16 = 0xffff;

const HEADER_BYTES = 24;
const BUNDLE_HEADER_BYTES = 4;

export const FrameKind = {
  Live: 1,
  Snapshot: 2,
} as const;
export type FrameKind = (typeof FrameKind)[keyof typeof FrameKind];

export class FrameError extends Error {
  override name = "FrameError";
}

/** One decoded tile frame. Array fields are views into the source buffer (do not mutate). */
export interface TileFrame {
  readonly kind: FrameKind;
  readonly zoom: number;
  readonly x: number;
  readonly y: number;
  /** Epoch milliseconds; `recorded_at` of point i is `baseTimeMs + deltaMs[i]`. */
  readonly baseTimeMs: number;
  readonly count: number;
  /** Latitude x 1e7. */
  readonly lat: Int32Array;
  /** Longitude x 1e7. */
  readonly lon: Int32Array;
  readonly deltaMs: Uint32Array;
  /** Centimetres per second, {@link UNKNOWN_U16} when the device did not report it. */
  readonly speed: Uint16Array;
  /** Centidegrees clockwise from north, {@link UNKNOWN_U16} when unknown. */
  readonly heading: Uint16Array;
  readonly ids: readonly string[];
}

/** A point of a frame converted to natural units (tests, inspectors; not the hot path). */
export interface FramePoint {
  deviceId: string;
  lat: number;
  lon: number;
  recordedAtMs: number;
  speedMps: number | null;
  headingDeg: number | null;
}

const LITTLE_ENDIAN_HOST = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
const utf8 = new TextDecoder("utf-8", { fatal: true });

function readU64(view: DataView, offset: number): number {
  const low = view.getUint32(offset, true);
  const high = view.getUint32(offset + 4, true);
  return high * 0x1_0000_0000 + low;
}

type ArrayCtor<T> = {
  new (buffer: ArrayBufferLike, byteOffset: number, length: number): T;
  new (length: number): T;
};

/**
 * A little-endian array view over `buffer`. On the (theoretical) big-endian host the values are
 * read through a DataView into a fresh array instead, so callers never see byte-swapped numbers.
 */
function leArray<T extends Int32Array | Uint32Array | Uint16Array>(
  ctor: ArrayCtor<T>,
  view: DataView,
  offset: number,
  count: number,
  read: (view: DataView, offset: number) => number,
  width: number,
): T {
  if (LITTLE_ENDIAN_HOST) {
    return new ctor(view.buffer, view.byteOffset + offset, count);
  }
  const values = new ctor(count);
  for (let i = 0; i < count; i++) {
    values[i] = read(view, offset + i * width);
  }
  return values;
}

/** Decode one tile frame from `bytes` (a view of exactly the frame, or starting at the frame). */
export function decodeTile(bytes: Uint8Array): TileFrame {
  if (bytes.byteLength < HEADER_BYTES) {
    throw new FrameError("frame shorter than its header");
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const magic = view.getUint8(0);
  const version = view.getUint8(1);
  if (magic !== FRAME_MAGIC || version !== FRAME_VERSION) {
    throw new FrameError(`unsupported frame magic/version 0x${magic.toString(16)}/${version}`);
  }
  if ((bytes.byteOffset & 3) !== 0) {
    // Frames inside a bundle are always aligned; a caller holding a misaligned slice gets a copy.
    return decodeTile(bytes.slice());
  }
  const kind = view.getUint8(2);
  if (kind !== FrameKind.Live && kind !== FrameKind.Snapshot) {
    throw new FrameError(`unknown frame kind ${kind}`);
  }
  const zoom = view.getUint8(3);
  const x = view.getUint32(4, true);
  const y = view.getUint32(8, true);
  const baseTimeMs = readU64(view, 12);
  const count = view.getUint32(20, true);

  let offset = HEADER_BYTES;
  if (bytes.byteLength < offset + 16 * count + 4) {
    throw new FrameError("frame truncated inside its arrays");
  }
  const lat = leArray(Int32Array, view, offset, count, (v, o) => v.getInt32(o, true), 4);
  offset += 4 * count;
  const lon = leArray(Int32Array, view, offset, count, (v, o) => v.getInt32(o, true), 4);
  offset += 4 * count;
  const deltaMs = leArray(Uint32Array, view, offset, count, (v, o) => v.getUint32(o, true), 4);
  offset += 4 * count;
  const speed = leArray(Uint16Array, view, offset, count, (v, o) => v.getUint16(o, true), 2);
  offset += 2 * count;
  const heading = leArray(Uint16Array, view, offset, count, (v, o) => v.getUint16(o, true), 2);
  offset += 2 * count;

  const idsLength = view.getUint32(offset, true);
  offset += 4;
  if (bytes.byteLength < offset + idsLength) {
    throw new FrameError("frame truncated inside its id blob");
  }
  let ids: string[] = [];
  if (count > 0) {
    let text: string;
    try {
      text = utf8.decode(bytes.subarray(offset, offset + idsLength));
    } catch {
      throw new FrameError("frame id blob is not valid UTF-8");
    }
    ids = text.split("\u0000");
  }
  if (ids.length !== count) {
    throw new FrameError(`frame declares ${count} devices but carries ${ids.length} ids`);
  }

  return {
    kind: kind as FrameKind,
    zoom,
    x,
    y,
    baseTimeMs,
    count,
    lat,
    lon,
    deltaMs,
    speed,
    heading,
    ids,
  };
}

/** Split a bundle into its tile frames (views into `buffer`, nothing is copied). */
export function decodeBundle(buffer: ArrayBuffer | Uint8Array): Uint8Array[] {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  if (bytes.byteLength < BUNDLE_HEADER_BYTES) {
    throw new FrameError("bundle shorter than its header");
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const magic = view.getUint8(0);
  const version = view.getUint8(1);
  if (magic !== BUNDLE_MAGIC || version !== FRAME_VERSION) {
    throw new FrameError(`unsupported bundle magic/version 0x${magic.toString(16)}/${version}`);
  }
  const count = view.getUint16(2, true);
  const frames: Uint8Array[] = [];
  let offset = BUNDLE_HEADER_BYTES;
  for (let i = 0; i < count; i++) {
    if (bytes.byteLength < offset + 4) {
      throw new FrameError("bundle truncated before a frame length");
    }
    const size = view.getUint32(offset, true);
    offset += 4;
    if (bytes.byteLength < offset + size) {
      throw new FrameError("bundle truncated inside a frame");
    }
    frames.push(bytes.subarray(offset, offset + size));
    offset += size;
  }
  return frames;
}

/** Decode every frame of a bundle. */
export function decodeBundleFrames(buffer: ArrayBuffer | Uint8Array): TileFrame[] {
  return decodeBundle(buffer).map(decodeTile);
}

/** Natural-unit view of point `i` of `frame`. */
export function pointAt(frame: TileFrame, i: number): FramePoint {
  const speed = frame.speed[i] ?? UNKNOWN_U16;
  const heading = frame.heading[i] ?? UNKNOWN_U16;
  return {
    deviceId: frame.ids[i] ?? "",
    lat: (frame.lat[i] ?? 0) / 1e7,
    lon: (frame.lon[i] ?? 0) / 1e7,
    recordedAtMs: frame.baseTimeMs + (frame.deltaMs[i] ?? 0),
    speedMps: speed === UNKNOWN_U16 ? null : speed / 100,
    headingDeg: heading === UNKNOWN_U16 ? null : heading / 100,
  };
}
