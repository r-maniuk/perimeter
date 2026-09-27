/**
 * The selected device's recent path: a line that fades in from its oldest point, so direction of
 * travel reads at a glance, extended live as new positions arrive.
 */
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import { setSourceData } from "./sources";

export const TRAIL_SOURCE = "trail";
const MAX_POINTS = 2_000;
/** Line width in CSS pixels at two zoom levels, linear in between (and flat outside). */
const TRAIL_WIDTH: readonly [[number, number], [number, number]] = [
  [10, 2],
  [16, 4],
];

/** The trail's width at `zoom`, for anything drawn to continue the line. */
export function trailWidth(zoom: number): number {
  const [[z0, w0], [z1, w1]] = TRAIL_WIDTH;
  const t = Math.min(1, Math.max(0, (zoom - z0) / (z1 - z0)));
  return w0 + (w1 - w0) * t;
}

export function addTrailLayer(map: MapLibreMap, color: string, beforeId?: string): void {
  map.addSource(TRAIL_SOURCE, {
    type: "geojson",
    lineMetrics: true,
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer(
    {
      id: "trail-line",
      type: "line",
      source: TRAIL_SOURCE,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-width": [
          "interpolate",
          ["linear"],
          ["zoom"],
          TRAIL_WIDTH[0][0],
          TRAIL_WIDTH[0][1],
          TRAIL_WIDTH[1][0],
          TRAIL_WIDTH[1][1],
        ],
        "line-gradient": trailGradient(color),
      },
    },
    beforeId,
  );
}

function trailGradient(color: string) {
  return [
    "interpolate",
    ["linear"],
    ["line-progress"],
    0,
    "rgba(0, 0, 0, 0)",
    0.35,
    withAlpha(color, 0.35),
    1,
    withAlpha(color, 0.95),
  ] as never;
}

export function setTrailColor(map: MapLibreMap, color: string): void {
  if (map.getLayer("trail-line")) {
    map.setPaintProperty("trail-line", "line-gradient", trailGradient(color));
  }
}

function withAlpha(hex: string, alpha: number): string {
  const n = Number.parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/**
 * Keeps the trail's coordinates and pushes them to the map.
 *
 * Live positions extend the line one report behind: the dot glides from the previous report
 * toward the newest one, so drawing the line up to the newest report would put its tip ahead of
 * the dot. The newest report joins the line when the next one arrives (the dot is there by then).
 */
export class TrailRenderer {
  #map: MapLibreMap;
  #coordinates: [number, number][] = [];
  #pending: [number, number] | null = null;
  #deviceId: string | null = null;

  constructor(map: MapLibreMap) {
    this.#map = map;
  }

  get deviceId(): string | null {
    return this.#deviceId;
  }

  /**
   * Where the drawn line ends. The device layer links it to the moving dot every frame: the line
   * itself only grows when a report arrives, so on its own it would trail a report behind.
   */
  end(): { deviceId: string; lon: number; lat: number } | null {
    const last = this.#coordinates.at(-1);
    if (!this.#deviceId || !last) return null;
    return { deviceId: this.#deviceId, lon: last[0], lat: last[1] };
  }

  show(deviceId: string, coordinates: [number, number][]): void {
    this.#deviceId = deviceId;
    const recent = coordinates.slice(-MAX_POINTS);
    // The newest report may still be ahead of the dot; it joins the line with the next one.
    this.#pending = recent.pop() ?? null;
    this.#coordinates = recent;
    this.#push();
  }

  /** The device reported `lon`/`lat`; the previous report joins the line. */
  extend(deviceId: string, lon: number, lat: number): void {
    if (deviceId !== this.#deviceId) return;
    const pending = this.#pending;
    if (pending && Math.abs(pending[0] - lon) < 1e-7 && Math.abs(pending[1] - lat) < 1e-7) return;
    this.#pending = [lon, lat];
    if (!pending) return;
    const last = this.#coordinates.at(-1);
    if (last && Math.abs(last[0] - pending[0]) < 1e-7 && Math.abs(last[1] - pending[1]) < 1e-7) {
      return;
    }
    this.#coordinates.push(pending);
    if (this.#coordinates.length > MAX_POINTS) this.#coordinates.shift();
    this.#push();
  }

  clear(): void {
    this.#deviceId = null;
    this.#coordinates = [];
    this.#pending = null;
    this.#push();
  }

  #push(): void {
    const source = this.#map.getSource(TRAIL_SOURCE) as GeoJSONSource | undefined;
    if (!source) return;
    setSourceData(
      source,
      this.#coordinates.length >= 2
        ? {
            type: "Feature",
            properties: {},
            geometry: { type: "LineString", coordinates: this.#coordinates },
          }
        : { type: "FeatureCollection", features: [] },
    );
  }
}
