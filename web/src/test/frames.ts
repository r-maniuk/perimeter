/**
 * Test-side encoder mirroring `perimeter/wire/frames.py`. The browser only decodes; tests build
 * frames with this and check it against the shared golden vectors, so both directions are pinned.
 */
import {
  BUNDLE_MAGIC,
  FRAME_MAGIC,
  FRAME_VERSION,
  type FrameKind,
  UNKNOWN_U16,
} from "@/lib/frames";

export interface EncodablePoint {
  deviceId: string;
  lat: number;
  lon: number;
  recordedAtMs: number;
  speedMps?: number | null;
  headingDeg?: number | null;
}

/** Python's round(): halves go to the even neighbour. */
export function roundHalfEven(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

function speedU16(value: number | null | undefined): number {
  if (value === null || value === undefined) return UNKNOWN_U16;
  return Math.min(Math.max(roundHalfEven(value * 100), 0), UNKNOWN_U16 - 1);
}

function headingU16(value: number | null | undefined): number {
  if (value === null || value === undefined) return UNKNOWN_U16;
  const centi = roundHalfEven(value * 100);
  return ((centi % 36_000) + 36_000) % 36_000;
}

export function encodeTile(
  kind: FrameKind,
  zoom: number,
  x: number,
  y: number,
  points: readonly EncodablePoint[],
): Uint8Array {
  const count = points.length;
  const base = count ? Math.min(...points.map((p) => p.recordedAtMs)) : 0;
  const ids = new TextEncoder().encode(points.map((p) => p.deviceId).join("\u0000"));
  const padding = (4 - (ids.length % 4)) % 4;
  const size = 24 + 16 * count + 4 + ids.length + padding;
  const out = new Uint8Array(size);
  const view = new DataView(out.buffer);
  view.setUint8(0, FRAME_MAGIC);
  view.setUint8(1, FRAME_VERSION);
  view.setUint8(2, kind);
  view.setUint8(3, zoom);
  view.setUint32(4, x, true);
  view.setUint32(8, y, true);
  view.setUint32(12, base % 0x1_0000_0000, true);
  view.setUint32(16, Math.floor(base / 0x1_0000_0000), true);
  view.setUint32(20, count, true);
  let offset = 24;
  for (const p of points) {
    view.setInt32(offset, roundHalfEven(p.lat * 1e7), true);
    offset += 4;
  }
  for (const p of points) {
    view.setInt32(offset, roundHalfEven(p.lon * 1e7), true);
    offset += 4;
  }
  for (const p of points) {
    view.setUint32(offset, p.recordedAtMs - base, true);
    offset += 4;
  }
  for (const p of points) {
    view.setUint16(offset, speedU16(p.speedMps), true);
    offset += 2;
  }
  for (const p of points) {
    view.setUint16(offset, headingU16(p.headingDeg), true);
    offset += 2;
  }
  view.setUint32(offset, ids.length, true);
  offset += 4;
  out.set(ids, offset);
  return out;
}

export function encodeBundle(frames: readonly Uint8Array[]): ArrayBuffer {
  const size = 4 + frames.reduce((sum, f) => sum + 4 + f.byteLength, 0);
  const out = new Uint8Array(size);
  const view = new DataView(out.buffer);
  view.setUint8(0, BUNDLE_MAGIC);
  view.setUint8(1, FRAME_VERSION);
  view.setUint16(2, frames.length, true);
  let offset = 4;
  for (const frame of frames) {
    view.setUint32(offset, frame.byteLength, true);
    offset += 4;
    out.set(frame, offset);
    offset += frame.byteLength;
  }
  return out.buffer;
}
