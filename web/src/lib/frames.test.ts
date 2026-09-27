import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { encodeBundle, encodeTile } from "@/test/frames";
import {
  decodeBundle,
  decodeBundleFrames,
  decodeTile,
  FrameError,
  FrameKind,
  pointAt,
  UNKNOWN_U16,
} from "./frames";

/** Golden vectors shared with the Python codec (`tests/golden/frames` at the repository root). */
const GOLDEN = resolve(import.meta.dirname, "../../../tests/golden/frames");

interface GoldenPoint {
  device_id: string;
  lat: number;
  lon: number;
  recorded_at_ms: number;
  speed_mps: number | null;
  heading_deg: number | null;
}

interface Golden {
  kind: FrameKind;
  tile: [number, number, number];
  points: GoldenPoint[];
}

function golden(name: string): { expected: Golden; bytes: Uint8Array } {
  const expected = JSON.parse(readFileSync(resolve(GOLDEN, `${name}.json`), "utf8")) as Golden;
  // Copy into a fresh ArrayBuffer so views start at offset 0, exactly like a WebSocket message.
  const bytes = new Uint8Array(readFileSync(resolve(GOLDEN, `${name}.bin`)));
  return { expected, bytes };
}

describe("golden vectors", () => {
  for (const name of ["amsterdam", "edge_values"]) {
    it(`decodes ${name}.bin exactly as the Python codec encoded it`, () => {
      const { expected, bytes } = golden(name);
      const frame = decodeTile(bytes);
      const [zoom, x, y] = expected.tile;
      expect({ kind: frame.kind, zoom: frame.zoom, x: frame.x, y: frame.y }).toEqual({
        kind: expected.kind,
        zoom,
        x,
        y,
      });
      expect(frame.count).toBe(expected.points.length);
      expected.points.forEach((want, i) => {
        const got = pointAt(frame, i);
        expect(got.deviceId).toBe(want.device_id);
        expect(Math.abs(got.lat - want.lat)).toBeLessThanOrEqual(5e-8);
        expect(Math.abs(got.lon - want.lon)).toBeLessThanOrEqual(5e-8);
        expect(got.recordedAtMs).toBe(want.recorded_at_ms);
        if (want.speed_mps === null) expect(got.speedMps).toBeNull();
        else expect(got.speedMps).toBeCloseTo(want.speed_mps, 6);
        if (want.heading_deg === null) expect(got.headingDeg).toBeNull();
        else expect(got.headingDeg).toBeCloseTo(want.heading_deg, 6);
      });
    });

    it(`re-encodes ${name}.json to the identical bytes`, () => {
      const { expected, bytes } = golden(name);
      const [zoom, x, y] = expected.tile;
      const encoded = encodeTile(
        expected.kind,
        zoom,
        x,
        y,
        expected.points.map((p) => ({
          deviceId: p.device_id,
          lat: p.lat,
          lon: p.lon,
          recordedAtMs: p.recorded_at_ms,
          speedMps: p.speed_mps,
          headingDeg: p.heading_deg,
        })),
      );
      expect(Array.from(encoded)).toEqual(Array.from(bytes));
    });
  }

  it("reads time deltas as unsigned 32-bit values", () => {
    const { bytes } = golden("edge_values");
    const frame = decodeTile(bytes);
    // The second point sits exactly 2^31 ms after the base: a signed read would go negative.
    expect(frame.deltaMs[1]).toBe(2 ** 31);
    expect(frame.speed[0]).toBe(UNKNOWN_U16 - 1);
    expect(frame.heading[1]).toBe(UNKNOWN_U16);
  });
});

describe("tile frames", () => {
  it("exposes aligned views without copying the buffer", () => {
    const frame = decodeTile(
      encodeTile(FrameKind.Live, 12, 1, 2, [
        { deviceId: "a", lat: 1, lon: 2, recordedAtMs: 1_000 },
        { deviceId: "b", lat: 3, lon: 4, recordedAtMs: 1_500, speedMps: 5, headingDeg: 90 },
      ]),
    );
    expect(frame.lat.buffer).toBe(frame.lon.buffer);
    expect(frame.lat.byteOffset).toBe(24);
    expect(Array.from(frame.lat)).toEqual([10_000_000, 30_000_000]);
    expect(Array.from(frame.deltaMs)).toEqual([0, 500]);
    expect(frame.ids).toEqual(["a", "b"]);
  });

  it("handles an empty frame", () => {
    const frame = decodeTile(encodeTile(FrameKind.Snapshot, 9, 3, 4, []));
    expect(frame.kind).toBe(FrameKind.Snapshot);
    expect(frame.count).toBe(0);
    expect(frame.ids).toEqual([]);
  });

  it("decodes a frame that starts at an unaligned offset by copying it", () => {
    const frame = encodeTile(FrameKind.Live, 12, 5, 6, [
      { deviceId: "x", lat: 52.1, lon: 4.2, recordedAtMs: 7 },
    ]);
    const shifted = new Uint8Array(frame.byteLength + 1);
    shifted.set(frame, 1);
    expect(pointAt(decodeTile(shifted.subarray(1)), 0).lat).toBeCloseTo(52.1, 7);
  });

  it("keeps multi-byte UTF-8 ids intact", () => {
    const frame = decodeTile(
      encodeTile(FrameKind.Live, 12, 0, 0, [
        { deviceId: "grachten-ü", lat: 0, lon: 0, recordedAtMs: 1 },
        { deviceId: "b", lat: 0, lon: 0, recordedAtMs: 2 },
      ]),
    );
    expect(frame.ids).toEqual(["grachten-ü", "b"]);
  });

  it.each([
    ["empty", new Uint8Array(0)],
    ["two bytes", Uint8Array.of(0xb7, 0x01)],
    ["wrong magic", new Uint8Array(24)],
    ["unknown version", Uint8Array.from([0xb7, 0x02, ...new Array(22).fill(0)])],
    [
      "arrays missing",
      Uint8Array.from([0xb7, 0x01, 0x01, 0x0c, ...new Array(16).fill(0), 5, 0, 0, 0]),
    ],
    ["unknown kind", Uint8Array.from([0xb7, 0x01, 0x07, 0x0c, ...new Array(20).fill(0)])],
  ])("rejects a malformed frame (%s)", (_, data) => {
    expect(() => decodeTile(data)).toThrow(FrameError);
  });

  it("rejects a frame truncated inside its id blob", () => {
    const data = encodeTile(FrameKind.Live, 3, 1, 1, [
      { deviceId: "abc", lat: 1, lon: 2, recordedAtMs: 5 },
    ]);
    expect(() => decodeTile(data.subarray(0, 30))).toThrow(FrameError);
    expect(() => decodeTile(data.subarray(0, data.byteLength - 4))).toThrow(FrameError);
  });

  it("rejects a frame whose id count disagrees with its header", () => {
    const data = encodeTile(FrameKind.Live, 3, 1, 1, [
      { deviceId: "a", lat: 1, lon: 2, recordedAtMs: 5 },
      { deviceId: "b", lat: 1, lon: 2, recordedAtMs: 5 },
    ]);
    // Replace the separator with a regular character: two declared devices, one id.
    const blobStart = 24 + 16 * 2 + 4;
    data[blobStart + 1] = "_".charCodeAt(0);
    expect(() => decodeTile(data)).toThrow(/declares 2 devices/);
  });
});

describe("bundles", () => {
  it("carries frames unchanged and in order", () => {
    const first = encodeTile(FrameKind.Live, 12, 1, 1, [
      { deviceId: "a", lat: 1, lon: 1, recordedAtMs: 1 },
    ]);
    const second = encodeTile(FrameKind.Snapshot, 12, 1, 2, []);
    const frames = decodeBundle(encodeBundle([first, second]));
    expect(frames.map((f) => Array.from(f))).toEqual([Array.from(first), Array.from(second)]);
    expect(decodeBundleFrames(encodeBundle([first, second])).map((f) => f.kind)).toEqual([
      FrameKind.Live,
      FrameKind.Snapshot,
    ]);
  });

  it("decodes a large bundle with every frame aligned", () => {
    const frames = Array.from({ length: 40 }, (_, t) =>
      encodeTile(
        FrameKind.Live,
        12,
        2100 + t,
        1346,
        Array.from({ length: 250 }, (_, i) => ({
          deviceId: `veh-${t}-${i}${"x".repeat(i % 3)}`,
          lat: 52.3 + i / 1e4,
          lon: 4.9 - i / 1e4,
          recordedAtMs: 1_790_000_000_000 + i,
          speedMps: i % 7 === 0 ? null : i / 10,
          headingDeg: i % 5 === 0 ? null : (i * 7) % 360,
        })),
      ),
    );
    const decoded = decodeBundleFrames(encodeBundle(frames));
    expect(decoded).toHaveLength(40);
    expect(decoded.reduce((n, f) => n + f.count, 0)).toBe(10_000);
    const last = decoded[39];
    expect(last && pointAt(last, 249).deviceId).toBe("veh-39-249");
  });

  it("rejects truncated or foreign bundles", () => {
    const bundle = new Uint8Array(
      encodeBundle([
        encodeTile(FrameKind.Live, 3, 1, 1, [{ deviceId: "a", lat: 0, lon: 0, recordedAtMs: 0 }]),
      ]),
    );
    expect(() => decodeBundle(bundle.subarray(0, bundle.byteLength - 4))).toThrow(FrameError);
    expect(() => decodeBundle(Uint8Array.of(0x00, 0x01, 0x00, 0x00))).toThrow(FrameError);
    expect(() => decodeBundle(Uint8Array.of(0xb8, 0x01, 0x01, 0x00))).toThrow(/frame length/);
    expect(() => decodeBundle(new Uint8Array(2))).toThrow(FrameError);
  });
});
