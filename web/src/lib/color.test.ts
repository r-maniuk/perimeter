import { describe, expect, it } from "vitest";
import {
  contrast,
  formatRgba,
  fromOklch,
  hexToRgba,
  mix,
  packRgba,
  parseColor,
  toHex,
  toOklch,
} from "./color";

describe("parseColor", () => {
  it.each([
    ["#fff", { r: 255, g: 255, b: 255, a: 1 }],
    ["#6d5dfc", { r: 109, g: 93, b: 252, a: 1 }],
    ["#6d5dfc80", { r: 109, g: 93, b: 252, a: 128 / 255 }],
    ["rgb(242,243,240)", { r: 242, g: 243, b: 240, a: 1 }],
    ["rgba(255, 255, 255, 0.7)", { r: 255, g: 255, b: 255, a: 0.7 }],
    ["rgb(10 20 30 / 50%)", { r: 10, g: 20, b: 30, a: 0.5 }],
    ["white", { r: 255, g: 255, b: 255, a: 1 }],
    ["transparent", { r: 0, g: 0, b: 0, a: 0 }],
  ])("parses %s", (input, expected) => {
    const got = parseColor(input);
    expect(got).not.toBeNull();
    expect(got?.r).toBeCloseTo(expected.r, 6);
    expect(got?.g).toBeCloseTo(expected.g, 6);
    expect(got?.b).toBeCloseTo(expected.b, 6);
    expect(got?.a).toBeCloseTo(expected.a, 6);
  });

  it("parses hsl forms", () => {
    expect(parseColor("hsl(0,0%,98%)")).toEqual({ r: 249.9, g: 249.9, b: 249.9, a: 1 });
    const violet = parseColor("hsla(246, 96%, 68%, 0.5)");
    expect(violet && toHex(violet)).toBe("#6f5ffc");
    expect(violet?.a).toBe(0.5);
  });

  it.each(["motorway", "get", "#12", "rgb(1,2)", "hsl(x, 1%, 2%)", ""])("rejects %j", (input) => {
    expect(parseColor(input)).toBeNull();
  });
});

describe("conversions", () => {
  it("round-trips through OKLCH", () => {
    for (const hex of ["#6d5dfc", "#f3f2ee", "#121216", "#0ca678", "#e8590c"]) {
      expect(toHex(fromOklch(toOklch(hexToRgba(hex))))).toBe(hex);
    }
  });

  it("formats, mixes and measures contrast", () => {
    expect(formatRgba({ r: 1.4, g: 254.6, b: 300, a: 0.12345 })).toBe("rgba(1, 255, 255, 0.123)");
    expect(toHex(mix(hexToRgba("#000000"), hexToRgba("#ffffff"), 0.5))).toBe("#808080");
    expect(contrast(hexToRgba("#000000"), hexToRgba("#ffffff"))).toBeCloseTo(21, 6);
  });

  it("packs RGBA little-endian for byte attributes", () => {
    const packed = packRgba({ r: 0x11, g: 0x22, b: 0x33, a: 1 });
    expect(packed).toBe(0xff332211);
    expect(Array.from(new Uint8Array(new Uint32Array([packed]).buffer))).toEqual([
      0x11, 0x22, 0x33, 0xff,
    ]);
  });
});
