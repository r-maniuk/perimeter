/**
 * Small colour toolkit: parse the CSS colour forms found in map styles, move through OKLCH for
 * perceptually even adjustments, and pack colours for the GPU.
 */

export interface Rgba {
  /** 0–255 */
  r: number;
  g: number;
  b: number;
  /** 0–1 */
  a: number;
}

export interface Oklch {
  l: number;
  c: number;
  /** Degrees. */
  h: number;
  a: number;
}

const NAMED: Record<string, Rgba> = {
  white: { r: 255, g: 255, b: 255, a: 1 },
  black: { r: 0, g: 0, b: 0, a: 1 },
  transparent: { r: 0, g: 0, b: 0, a: 0 },
};

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const clamp255 = (v: number) => Math.min(255, Math.max(0, v));

function parseHex(hex: string): Rgba | null {
  const body = hex.slice(1);
  if (!/^[0-9a-f]+$/i.test(body)) return null;
  const expand = (s: string) => (s.length === 1 ? s + s : s);
  if (body.length === 3 || body.length === 4) {
    const [r, g, b, a = "f"] = body.split("") as [string, string, string, string?];
    return {
      r: Number.parseInt(expand(r), 16),
      g: Number.parseInt(expand(g), 16),
      b: Number.parseInt(expand(b), 16),
      a: Number.parseInt(expand(a), 16) / 255,
    };
  }
  if (body.length === 6 || body.length === 8) {
    return {
      r: Number.parseInt(body.slice(0, 2), 16),
      g: Number.parseInt(body.slice(2, 4), 16),
      b: Number.parseInt(body.slice(4, 6), 16),
      a: body.length === 8 ? Number.parseInt(body.slice(6, 8), 16) / 255 : 1,
    };
  }
  return null;
}

function parseComponent(token: string, scale: number): number | null {
  const value = Number.parseFloat(token);
  if (Number.isNaN(value)) return null;
  return token.trim().endsWith("%") ? (value / 100) * scale : value;
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const hue = (((h % 360) + 360) % 360) / 360;
  if (s === 0) return [l * 255, l * 255, l * 255];
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const channel = (t0: number) => {
    let t = t0;
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  return [channel(hue + 1 / 3) * 255, channel(hue) * 255, channel(hue - 1 / 3) * 255];
}

/** Parse `#hex`, `rgb[a](…)`, `hsl[a](…)` and a few names; `null` for anything else. */
export function parseColor(input: string): Rgba | null {
  const text = input.trim().toLowerCase();
  if (text in NAMED) return { ...(NAMED[text] as Rgba) };
  if (text.startsWith("#")) return parseHex(text);
  const match = /^(rgba?|hsla?)\(([^)]*)\)$/.exec(text);
  if (!match) return null;
  const [, fn, args = ""] = match;
  const parts = args
    .split(/[\s,/]+/)
    .map((p) => p.trim())
    .filter(Boolean);
  if (parts.length !== 3 && parts.length !== 4) return null;
  const alpha = parts[3] === undefined ? 1 : parseComponent(parts[3], 1);
  if (alpha === null) return null;
  if (fn?.startsWith("rgb")) {
    const [r, g, b] = parts.slice(0, 3).map((p) => parseComponent(p, 255));
    if (r == null || g == null || b == null) return null;
    return { r: clamp255(r), g: clamp255(g), b: clamp255(b), a: clamp01(alpha) };
  }
  const h = Number.parseFloat(parts[0] ?? "");
  const s = parseComponent(parts[1] ?? "", 1);
  const l = parseComponent(parts[2] ?? "", 1);
  if (Number.isNaN(h) || s === null || l === null) return null;
  const [r, g, b] = hslToRgb(h, clamp01(s), clamp01(l));
  return { r, g, b, a: clamp01(alpha) };
}

export function formatRgba({ r, g, b, a }: Rgba): string {
  const round = (v: number) => Math.round(clamp255(v));
  const alpha = Math.round(clamp01(a) * 1000) / 1000;
  return `rgba(${round(r)}, ${round(g)}, ${round(b)}, ${alpha})`;
}

export function toHex({ r, g, b }: Rgba): string {
  const hex = (v: number) => Math.round(clamp255(v)).toString(16).padStart(2, "0");
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

const toLinear = (c: number) => {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};
const fromLinear = (v: number) => {
  const c = v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055;
  return clamp255(c * 255);
};

export function toOklch({ r, g, b, a }: Rgba): Oklch {
  const lr = toLinear(r);
  const lg = toLinear(g);
  const lb = toLinear(b);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  const c = Math.hypot(A, B);
  const h = c < 1e-6 ? 0 : ((Math.atan2(B, A) * 180) / Math.PI + 360) % 360;
  return { l: L, c, h, a };
}

export function fromOklch({ l, c, h, a }: Oklch): Rgba {
  const hr = (h * Math.PI) / 180;
  const A = c * Math.cos(hr);
  const B = c * Math.sin(hr);
  const l_ = (l + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m_ = (l - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s_ = (l - 0.0894841775 * A - 1.291485548 * B) ** 3;
  return {
    r: fromLinear(4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_),
    g: fromLinear(-1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_),
    b: fromLinear(-0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_),
    a,
  };
}

export function mix(from: Rgba, to: Rgba, t: number): Rgba {
  return {
    r: from.r + (to.r - from.r) * t,
    g: from.g + (to.g - from.g) * t,
    b: from.b + (to.b - from.b) * t,
    a: from.a + (to.a - from.a) * t,
  };
}

/** Relative luminance (WCAG) of an sRGB colour. */
export function luminance({ r, g, b }: Rgba): number {
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

/** WCAG contrast ratio between two opaque colours. */
export function contrast(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** RGBA packed little-endian into a u32 (byte order r, g, b, a) for `UNSIGNED_BYTE` attributes. */
export function packRgba({ r, g, b, a }: Rgba): number {
  return (
    ((Math.round(clamp01(a) * 255) << 24) >>> 0) +
    (Math.round(clamp255(b)) << 16) +
    (Math.round(clamp255(g)) << 8) +
    Math.round(clamp255(r))
  );
}

export function hexToRgba(hex: string): Rgba {
  return parseHex(hex.trim()) ?? { r: 109, g: 93, b: 252, a: 1 };
}
