/** Display formatting: compact, precise, locale-aware where it matters. */

const integer = new Intl.NumberFormat("en", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const oneDecimal = new Intl.NumberFormat("en", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});
const clock = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const clockShort = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });

export function formatCount(value: number): string {
  return Math.abs(value) >= 10_000 ? compact.format(value) : integer.format(value);
}

/** Rates per second: "3.3K/s", "12/s", "0.4/s". */
export function formatRate(perSecond: number): string {
  if (!Number.isFinite(perSecond)) return "–";
  if (perSecond >= 1_000) return `${compact.format(perSecond)}/s`;
  if (perSecond >= 10) return `${integer.format(perSecond)}/s`;
  return `${oneDecimal.format(perSecond)}/s`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "–";
  if (ms >= 0 && ms < 1) return "<1 ms";
  if (ms >= 10_000) return `${oneDecimal.format(ms / 1000)} s`;
  if (ms >= 10) return `${integer.format(ms)} ms`;
  return `${oneDecimal.format(ms)} ms`;
}

/** Distances: "850 m", "1.24 km", "12.5 km", "100 km". */
export function formatDistance(meters: number): string {
  if (meters < 1_000) return `${integer.format(meters)} m`;
  const km = meters / 1_000;
  if (km < 10) return `${km.toFixed(2)} km`;
  if (km < 100) return `${km.toFixed(1)} km`;
  return `${integer.format(km)} km`;
}

export function formatSpeed(mps: number | null | undefined): string {
  if (mps === null || mps === undefined || !Number.isFinite(mps)) return "–";
  const kmh = mps * 3.6;
  return `${kmh < 10 ? oneDecimal.format(kmh) : integer.format(kmh)} km/h`;
}

const POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] as const;

export function compassPoint(degrees: number): string {
  const index = Math.round((((degrees % 360) + 360) % 360) / 45) % 8;
  return POINTS[index] ?? "N";
}

export function formatHeading(degrees: number | null | undefined): string {
  if (degrees === null || degrees === undefined || !Number.isFinite(degrees)) return "–";
  return `${compassPoint(degrees)} ${integer.format(((degrees % 360) + 360) % 360)}°`;
}

/** "now", "8 s ago", "3 min ago", "2 h ago", "4 d ago". */
export function formatAge(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 3) return "now";
  if (s < 60) return `${s} s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.floor(h / 24)} d ago`;
}

export function formatClock(epochMs: number): string {
  return clock.format(epochMs);
}

export function formatClockShort(epochMs: number): string {
  return clockShort.format(epochMs);
}

/** "Today", "Yesterday" or "26 Sep", relative to `now`. */
export function formatDay(epochMs: number, now: number = Date.now()): string {
  const startOf = (t: number) => {
    const d = new Date(t);
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  };
  const diff = Math.round((startOf(now) - startOf(epochMs)) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return day.format(epochMs);
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${integer.format(seconds)} s`;
  if (seconds < 3_600) {
    const m = seconds / 60;
    return `${Number.isInteger(m) ? m : m.toFixed(1)} min`;
  }
  const h = seconds / 3_600;
  return `${Number.isInteger(h) ? h : h.toFixed(1)} h`;
}

export function formatCoordinate(lat: number, lon: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(5)}° ${ns}  ${Math.abs(lon).toFixed(5)}° ${ew}`;
}

/**
 * Parse a distance typed by a person: "850", "850 m", "1.2 km", "1,5km", "2k". Plain numbers are
 * metres. Returns `null` for anything else.
 */
export function parseDistance(input: string): number | null {
  const match = /^\s*(\d+(?:[.,]\d+)?)\s*(m|km|k)?\s*$/i.exec(input);
  if (!match) return null;
  const value = Number((match[1] ?? "").replace(",", "."));
  if (!Number.isFinite(value)) return null;
  const unit = (match[2] ?? "m").toLowerCase();
  return unit === "m" ? value : value * 1_000;
}

export type Axis = "lat" | "lon";

const AXIS_LIMIT: Record<Axis, number> = { lat: 90, lon: 180 };
const HEMISPHERES: Record<Axis, string> = { lat: "NS", lon: "EW" };
const DEGREES = String.raw`([+-]?\d+(?:[.,]\d+)?)\s*°?\s*([NSEW])?`;

function degrees(number: string, hemisphere: string | undefined, axis: Axis): number | null {
  let value = Number(number.replace(",", "."));
  if (!Number.isFinite(value)) return null;
  if (hemisphere) {
    const letter = hemisphere.toUpperCase();
    // "4.9 N" is not a longitude, and "-4.9 W" could mean either side.
    if (!HEMISPHERES[axis].includes(letter) || value < 0) return null;
    if (letter === "S" || letter === "W") value = -value;
  }
  return Math.abs(value) <= AXIS_LIMIT[axis] ? value : null;
}

/**
 * Parse a latitude or longitude typed by a person, in decimal degrees: "52.3731", "52,3731",
 * "-4.5", "52.3731° N", "4.89 W". Returns `null` for anything else or anything out of range.
 */
export function parseDegrees(input: string, axis: Axis): number | null {
  const match = new RegExp(`^\\s*${DEGREES}\\s*$`, "i").exec(input);
  if (!match) return null;
  return degrees(match[1] ?? "", match[2], axis);
}

/**
 * Parse a latitude/longitude pair as maps copy it: "52.3731, 4.8926", "52.3731 4.8926",
 * "52.37310° N  4.89260° E". Decimal commas are only read in a single value, never in a pair.
 */
export function parseCoordinatePair(input: string): { lat: number; lon: number } | null {
  const part = String.raw`([+-]?\d+(?:\.\d+)?)\s*°?\s*([NSEW])?`;
  const match = new RegExp(`^\\s*${part}\\s*(?:[,;]\\s*|\\s+)${part}\\s*$`, "i").exec(input);
  if (!match) return null;
  const lat = degrees(match[1] ?? "", match[2], "lat");
  const lon = degrees(match[3] ?? "", match[4], "lon");
  return lat === null || lon === null ? null : { lat, lon };
}
