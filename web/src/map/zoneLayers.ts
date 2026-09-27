/**
 * Zones on the map: geodesic circles as fill + outline layers, name labels on their northern rim,
 * a "breath" when devices report from inside, and animated arrivals, edits and removals.
 *
 * Circle polygons are computed here, for display only, from the zone's centre and radius on the
 * WGS84 ellipsoid (the same model the database decides containment with). Rendering state that
 * changes every frame — breathing, selection — lives in feature-state so the geometry is only
 * re-sent when a zone actually moves.
 */
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import { circleRing, destination, segmentsFor } from "@/lib/geodesy";
import { LABEL_FONT_BOLD } from "./basemap";
import { setSourceData } from "./sources";

export const ZONE_SOURCE = "zones";
export const ZONE_LABEL_SOURCE = "zone-labels";
export const DRAFT_SOURCE = "zone-draft";
export const ZONE_FILL_LAYER = "zones-fill";

export interface ZoneGeometry {
  id: string;
  name: string;
  color: string;
  lat: number;
  lon: number;
  radiusM: number;
  active: boolean;
}

interface Animation {
  from: ZoneGeometry & { opacity: number };
  to: ZoneGeometry & { opacity: number };
  start: number;
  duration: number;
  removeAtEnd: boolean;
}

type Feature = GeoJSON.Feature<GeoJSON.Polygon, Record<string, unknown>>;

const BREATH_S = 1.5;
const BREATH_FRAME_MS = 33;
const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

export function zoneLayersTheme(dark: boolean) {
  return {
    label: dark ? "#e6e5ef" : "#23222c",
    halo: dark ? "rgba(18, 18, 22, 0.92)" : "rgba(247, 246, 243, 0.95)",
    draft: dark ? "#8b7fff" : "#6d5dfc",
  };
}

export function addZoneLayers(map: MapLibreMap, dark: boolean, beforeId?: string): void {
  const theme = zoneLayersTheme(dark);
  map.addSource(ZONE_SOURCE, { type: "geojson", data: EMPTY, promoteId: "id" });
  map.addSource(ZONE_LABEL_SOURCE, { type: "geojson", data: EMPTY, promoteId: "id" });
  map.addSource(DRAFT_SOURCE, { type: "geojson", data: EMPTY });

  const breath = ["coalesce", ["feature-state", "breath"], 0];
  const selected = ["case", ["boolean", ["feature-state", "selected"], false], 1, 0];
  const hovered = ["case", ["boolean", ["feature-state", "hover"], false], 1, 0];
  const opacity = ["coalesce", ["get", "opacity"], 1];

  map.addLayer(
    {
      id: ZONE_FILL_LAYER,
      type: "fill",
      source: ZONE_SOURCE,
      paint: {
        "fill-color": ["get", "color"],
        "fill-opacity": [
          "*",
          opacity,
          [
            "case",
            ["get", "active"],
            ["+", 0.075, ["*", 0.11, breath], ["*", 0.05, selected], ["*", 0.03, hovered]],
            ["+", 0.03, ["*", 0.03, selected]],
          ],
        ] as never,
      },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: "zones-glow",
      type: "line",
      source: ZONE_SOURCE,
      filter: ["get", "active"],
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["+", 6, ["*", 8, breath]] as never,
        "line-blur": 6,
        "line-opacity": ["*", opacity, ["+", ["*", 0.45, breath], ["*", 0.25, selected]]] as never,
      },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: "zones-outline",
      type: "line",
      source: ZONE_SOURCE,
      filter: ["get", "active"],
      layout: { "line-join": "round" },
      paint: {
        "line-color": ["get", "color"],
        // Zoom may only drive a top-level interpolation, so the emphasis terms ride on each stop.
        "line-width": [
          "interpolate",
          ["linear"],
          ["zoom"],
          9,
          ["+", 1.1, ["*", 1.2, selected], ["*", 0.6, hovered], ["*", 0.8, breath]],
          15,
          ["+", 1.8, ["*", 1.2, selected], ["*", 0.6, hovered], ["*", 0.8, breath]],
        ] as never,
        "line-opacity": ["*", opacity, 0.95] as never,
      },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: "zones-outline-paused",
      type: "line",
      source: ZONE_SOURCE,
      filter: ["!", ["get", "active"]],
      layout: { "line-join": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["+", 1.2, ["*", 1, selected]] as never,
        "line-dasharray": [2, 2],
        "line-opacity": ["*", opacity, 0.75] as never,
      },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: "zones-draft-fill",
      type: "fill",
      source: DRAFT_SOURCE,
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": theme.draft, "fill-opacity": 0.1 },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: "zones-draft-line",
      type: "line",
      source: DRAFT_SOURCE,
      layout: { "line-cap": "round" },
      paint: {
        "line-color": theme.draft,
        "line-width": 1.6,
        "line-dasharray": [2.5, 1.5],
      },
    },
    beforeId,
  );
}

/** Labels go above the device layer so names stay readable over dense traffic. */
export function addZoneLabelLayer(map: MapLibreMap, dark: boolean, beforeId?: string): void {
  const theme = zoneLayersTheme(dark);
  map.addLayer(
    {
      id: "zones-label",
      type: "symbol",
      source: ZONE_LABEL_SOURCE,
      minzoom: 9,
      layout: {
        "text-field": ["get", "name"],
        "text-font": LABEL_FONT_BOLD,
        "text-size": ["interpolate", ["linear"], ["zoom"], 10, 11, 16, 13],
        "text-anchor": "bottom",
        "text-offset": [0, -0.35],
        "text-max-width": 12,
        "text-allow-overlap": false,
        "text-padding": 6,
      },
      paint: {
        "text-color": theme.label,
        "text-halo-color": theme.halo,
        "text-halo-width": 1.6,
        "text-opacity": ["coalesce", ["get", "opacity"], 1] as never,
      },
    },
    beforeId,
  );
}

export function applyZoneTheme(map: MapLibreMap, dark: boolean): void {
  const theme = zoneLayersTheme(dark);
  if (map.getLayer("zones-label")) {
    map.setPaintProperty("zones-label", "text-color", theme.label);
    map.setPaintProperty("zones-label", "text-halo-color", theme.halo);
  }
  if (map.getLayer("zones-draft-fill")) {
    map.setPaintProperty("zones-draft-fill", "fill-color", theme.draft);
    map.setPaintProperty("zones-draft-line", "line-color", theme.draft);
  }
}

const ringCache = new Map<string, [number, number][]>();

function ringFor(z: { lat: number; lon: number; radiusM: number }): [number, number][] {
  const key = `${z.lat.toFixed(8)},${z.lon.toFixed(8)},${z.radiusM.toFixed(2)}`;
  let ring = ringCache.get(key);
  if (!ring) {
    ring = circleRing({ lat: z.lat, lon: z.lon }, z.radiusM, segmentsFor(z.radiusM));
    if (ringCache.size > 512) ringCache.clear();
    ringCache.set(key, ring);
  }
  return ring;
}

function polygon(z: ZoneGeometry, opacity = 1): Feature {
  return {
    type: "Feature",
    id: z.id,
    properties: { id: z.id, name: z.name, color: z.color, active: z.active, opacity },
    geometry: { type: "Polygon", coordinates: [ringFor(z)] },
  };
}

function labelPoint(z: ZoneGeometry, opacity = 1): GeoJSON.Feature<GeoJSON.Point> {
  const top = destination({ lat: z.lat, lon: z.lon }, 0, z.radiusM);
  return {
    type: "Feature",
    id: z.id,
    properties: { id: z.id, name: z.name, opacity },
    geometry: { type: "Point", coordinates: [z.lon, top.lat] },
  };
}

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

const easeOut = (t: number) => 1 - (1 - t) ** 3;

/**
 * Owns the zone sources: merges server zones, a live drag preview and animations into GeoJSON,
 * and drives the per-frame effects (breathing) with its own animation loop while needed.
 */
export class ZoneRenderer {
  #map: MapLibreMap;
  #zones = new Map<string, ZoneGeometry>();
  #preview: ZoneGeometry | null = null;
  #animations = new Map<string, Animation>();
  #breaths = new Map<string, number>();
  #selected: string | null = null;
  #hovered: string | null = null;
  #frame = 0;
  #lastBreath = 0;
  #dirty = false;
  #reducedMotion: () => boolean;

  constructor(map: MapLibreMap, reducedMotion: () => boolean) {
    this.#map = map;
    this.#reducedMotion = reducedMotion;
  }

  /** Replace the zone set. `animate` lists ids changed elsewhere, which ease into place. */
  setZones(zones: readonly ZoneGeometry[], animate: ReadonlySet<string> = new Set()): void {
    const next = new Map(zones.map((z) => [z.id, z]));
    const now = performance.now();
    const motion = !this.#reducedMotion();
    if (motion) {
      for (const [id, z] of next) {
        if (!animate.has(id)) continue;
        const before = this.#zones.get(id);
        const from = before
          ? { ...this.#current(before, now), opacity: 1 }
          : { ...z, radiusM: z.radiusM * 0.2, opacity: 0 };
        this.#animations.set(id, {
          from,
          to: { ...z, opacity: 1 },
          start: now,
          duration: before ? 650 : 750,
          removeAtEnd: false,
        });
      }
      for (const [id, z] of this.#zones) {
        if (!next.has(id) && animate.has(id)) {
          this.#animations.set(id, {
            from: { ...this.#current(z, now), opacity: 1 },
            to: { ...z, radiusM: z.radiusM * 0.6, opacity: 0 },
            start: now,
            duration: 450,
            removeAtEnd: true,
          });
        }
      }
    }
    this.#zones = next;
    this.#dirty = true;
    this.#schedule();
  }

  /** Geometry shown while a zone is being dragged (not yet saved). */
  setPreview(preview: ZoneGeometry | null): void {
    this.#preview = preview;
    this.#dirty = true;
    this.#schedule();
  }

  setDraft(draft: { lat: number; lon: number; radiusM: number; edge?: [number, number] } | null) {
    const source = this.#map.getSource(DRAFT_SOURCE) as GeoJSONSource | undefined;
    if (!source) return;
    if (!draft) {
      setSourceData(source, EMPTY);
      return;
    }
    const features: GeoJSON.Feature[] = [
      {
        type: "Feature",
        properties: {},
        geometry: { type: "Polygon", coordinates: [ringFor(draft)] },
      },
    ];
    if (draft.edge) {
      features.push({
        type: "Feature",
        properties: {},
        geometry: { type: "LineString", coordinates: [[draft.lon, draft.lat], draft.edge] },
      });
    }
    setSourceData(source, { type: "FeatureCollection", features });
  }

  select(id: string | null): void {
    if (this.#selected === id) return;
    if (this.#selected) this.#state(this.#selected, { selected: false });
    this.#selected = id;
    if (id) this.#state(id, { selected: true });
  }

  hover(id: string | null): void {
    if (this.#hovered === id) return;
    if (this.#hovered) this.#state(this.#hovered, { hover: false });
    this.#hovered = id;
    if (id) this.#state(id, { hover: true });
  }

  /** Devices reported from inside these zones: let them breathe (calmly — not on every pulse). */
  pulse(zoneIds: Iterable<string>): void {
    if (this.#reducedMotion()) return;
    const now = performance.now();
    for (const id of zoneIds) {
      const started = this.#breaths.get(id);
      if (started !== undefined && now - started < BREATH_S * 1000 * 1.6) continue;
      if (!this.#zones.has(id)) continue;
      this.#breaths.set(id, now);
    }
    this.#schedule();
  }

  destroy(): void {
    cancelAnimationFrame(this.#frame);
    this.#frame = 0;
  }

  #state(id: string, state: Record<string, unknown>): void {
    if (!this.#map.getSource(ZONE_SOURCE)) return;
    this.#map.setFeatureState({ source: ZONE_SOURCE, id }, state);
  }

  #current(z: ZoneGeometry, now: number): ZoneGeometry {
    const animation = this.#animations.get(z.id);
    if (!animation) return z;
    const t = easeOut(Math.min(1, (now - animation.start) / animation.duration));
    return {
      ...animation.to,
      lat: lerp(animation.from.lat, animation.to.lat, t),
      lon: lerp(animation.from.lon, animation.to.lon, t),
      radiusM: lerp(animation.from.radiusM, animation.to.radiusM, t),
    };
  }

  #schedule(): void {
    if (this.#frame) return;
    this.#frame = requestAnimationFrame(() => this.#tick());
  }

  #tick(): void {
    this.#frame = 0;
    const now = performance.now();
    let again = false;

    if (this.#animations.size > 0) {
      this.#dirty = true;
      for (const [id, animation] of this.#animations) {
        if (now - animation.start >= animation.duration) this.#animations.delete(id);
        else again = true;
      }
    }
    if (this.#dirty) {
      this.#flush(now);
      this.#dirty = false;
    }
    // Breathing is a slow, soft effect: 30 updates a second are plenty, and every update costs a
    // full map repaint.
    const breathe = now - this.#lastBreath >= BREATH_FRAME_MS;
    if (breathe) this.#lastBreath = now;
    for (const [id, started] of this.#breaths) {
      const t = (now - started) / (BREATH_S * 1000);
      if (t >= 1) {
        this.#breaths.delete(id);
        this.#state(id, { breath: 0 });
      } else {
        if (breathe) this.#state(id, { breath: Math.sin(Math.PI * t) ** 2 });
        again = true;
      }
    }
    if (again) this.#schedule();
  }

  #flush(now: number): void {
    const zones = this.#map.getSource(ZONE_SOURCE) as GeoJSONSource | undefined;
    const labels = this.#map.getSource(ZONE_LABEL_SOURCE) as GeoJSONSource | undefined;
    if (!zones || !labels) return;
    const polygons: Feature[] = [];
    const points: GeoJSON.Feature<GeoJSON.Point>[] = [];
    const seen = new Set<string>();
    for (const z of this.#zones.values()) {
      seen.add(z.id);
      const shown = this.#preview?.id === z.id ? this.#preview : z;
      const animation = this.#animations.get(z.id);
      const opacity = animation
        ? lerp(
            animation.from.opacity,
            animation.to.opacity,
            easeOut(Math.min(1, (now - animation.start) / animation.duration)),
          )
        : 1;
      const geometry = animation ? this.#current(shown, now) : shown;
      polygons.push(polygon(geometry, opacity));
      points.push(labelPoint(geometry, opacity));
    }
    for (const [id, animation] of this.#animations) {
      if (seen.has(id) || !animation.removeAtEnd) continue;
      const t = easeOut(Math.min(1, (now - animation.start) / animation.duration));
      const geometry: ZoneGeometry = {
        ...animation.to,
        radiusM: lerp(animation.from.radiusM, animation.to.radiusM, t),
      };
      polygons.push(polygon(geometry, lerp(1, 0, t)));
    }
    setSourceData(zones, { type: "FeatureCollection", features: polygons });
    setSourceData(labels, { type: "FeatureCollection", features: points });
  }
}
