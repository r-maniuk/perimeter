/**
 * The basemap: OpenFreeMap's vector tiles (OpenMapTiles schema, no key) under our own palette.
 *
 * The published "positron" style is the structural starting point — its layer stack, filters and
 * zoom curves are well tuned — and is restyled programmatically: each layer family is repainted
 * from a small set of palette tokens, and anything unrecognised (a layer added upstream later) is
 * passed through the palette's fallback transform so it still fits the theme. Both themes are
 * derived from the same base, so switching theme only changes paint values: it is applied in
 * place, without reloading the style or disturbing the data layers drawn on top.
 */
import type { LayerSpecification, Map as MapLibreMap, StyleSpecification } from "maplibre-gl";
import { formatRgba, fromOklch, parseColor, type Rgba, toOklch } from "@/lib/color";

export const BASEMAP_STYLE_URL = "https://tiles.openfreemap.org/styles/positron";
export const LABEL_FONT = ["Noto Sans Regular"];
export const LABEL_FONT_BOLD = ["Noto Sans Bold"];

export type Theme = "light" | "dark";

export interface BasemapPalette {
  land: string;
  residential: string;
  park: string;
  wood: string;
  ice: string;
  water: string;
  waterway: string;
  building: string;
  buildingOutline: string;
  aeroway: string;
  runway: string;
  roadCasing: string;
  road: string;
  roadMinor: string;
  path: string;
  rail: string;
  railDash: string;
  boundary: string;
  label: string;
  labelMuted: string;
  labelWater: string;
  labelRoad: string;
  halo: string;
  /** Applied to colours of layers no rule knows about; `null` keeps them as published. */
  fallback: ((color: Rgba) => Rgba) | null;
}

/** Warm paper land, cool lavender water, ink labels: a quiet stage for live data. */
export const LIGHT_PALETTE: BasemapPalette = {
  land: "#f3f2ee",
  residential: "#efede8",
  park: "#e4eadf",
  wood: "#dde5d7",
  ice: "#f8f8f6",
  water: "#d2d7e9",
  waterway: "#c4cbe2",
  building: "#e8e6e0",
  buildingOutline: "#dcd9d1",
  aeroway: "#ebe9e3",
  runway: "#e0ddd6",
  roadCasing: "#dcd9d1",
  road: "#ffffff",
  roadMinor: "#fbfaf8",
  path: "#e7e5df",
  rail: "#d6d3cc",
  railDash: "#f7f6f2",
  boundary: "#bab6c8",
  label: "#2d2c37",
  labelMuted: "#6c6a79",
  labelWater: "#6b74a6",
  labelRoad: "#8b8995",
  halo: "#f7f6f3",
  fallback: null,
};

/**
 * Graphite and ink with violet-tinted water. Unknown layers are mapped by inverting OKLCH
 * lightness into the graphite range and muting chroma, which keeps their relative contrast.
 */
export const DARK_PALETTE: BasemapPalette = {
  land: "#121216",
  residential: "#15151a",
  park: "#141a17",
  wood: "#131915",
  ice: "#18181d",
  water: "#1c1a31",
  waterway: "#242043",
  building: "#1b1b21",
  buildingOutline: "#23232a",
  aeroway: "#18181e",
  runway: "#202027",
  roadCasing: "#1e1e24",
  road: "#2c2c35",
  roadMinor: "#24242b",
  path: "#1f1f25",
  rail: "#27272e",
  railDash: "#15151a",
  boundary: "#3c3850",
  label: "#cfcedc",
  labelMuted: "#8f8da0",
  labelWater: "#8a84d6",
  labelRoad: "#7d7b8b",
  halo: "#121216",
  fallback: (color) => {
    const c = toOklch(color);
    return fromOklch({ l: 0.16 + (1 - c.l) * 0.55, c: c.c * 0.6, h: c.h, a: c.a });
  },
};

export const PALETTES: Record<Theme, BasemapPalette> = { light: LIGHT_PALETTE, dark: DARK_PALETTE };

type Paint = Record<string, unknown>;

interface Rule {
  test: (id: string) => boolean;
  paint?: (p: BasemapPalette) => Paint;
  layout?: Paint;
}

const is =
  (...ids: string[]) =>
  (id: string) =>
    ids.includes(id);
const startsWith = (prefix: string) => (id: string) => id.startsWith(prefix);
const endsWith = (suffix: string) => (id: string) => id.endsWith(suffix);

const RULES: Rule[] = [
  { test: is("background"), paint: (p) => ({ "background-color": p.land }) },
  { test: is("park"), paint: (p) => ({ "fill-color": p.park }) },
  { test: is("water"), paint: (p) => ({ "fill-color": p.water }) },
  {
    test: is("landcover_ice_shelf", "landcover_glacier"),
    paint: (p) => ({ "fill-color": p.ice }),
  },
  { test: is("landuse_residential"), paint: (p) => ({ "fill-color": p.residential }) },
  { test: is("landcover_wood"), paint: (p) => ({ "fill-color": p.wood }) },
  { test: is("waterway"), paint: (p) => ({ "line-color": p.waterway }) },
  {
    test: is("building"),
    paint: (p) => ({ "fill-color": p.building, "fill-outline-color": p.buildingOutline }),
  },
  { test: is("aeroway-area"), paint: (p) => ({ "fill-color": p.aeroway }) },
  { test: is("aeroway-runway"), paint: (p) => ({ "line-color": p.aeroway }) },
  { test: startsWith("aeroway-"), paint: (p) => ({ "line-color": p.runway }) },
  { test: is("road_area_pier"), paint: (p) => ({ "fill-color": p.land }) },
  { test: is("road_pier"), paint: (p) => ({ "line-color": p.land }) },
  { test: is("highway_path"), paint: (p) => ({ "line-color": p.path }) },
  { test: is("highway_minor"), paint: (p) => ({ "line-color": p.roadMinor }) },
  { test: endsWith("_dashline"), paint: (p) => ({ "line-color": p.railDash }) },
  { test: startsWith("railway"), paint: (p) => ({ "line-color": p.rail }) },
  { test: endsWith("_casing"), paint: (p) => ({ "line-color": p.roadCasing }) },
  { test: endsWith("_subtle"), paint: (p) => ({ "line-color": p.roadCasing }) },
  { test: is("tunnel_motorway_inner"), paint: (p) => ({ "line-color": p.roadMinor }) },
  { test: endsWith("_inner"), paint: (p) => ({ "line-color": p.road }) },
  { test: startsWith("boundary"), paint: (p) => ({ "line-color": p.boundary }) },
  {
    test: (id) => id.startsWith("water_name") || id === "waterway_line_label",
    paint: (p) => ({ "text-color": p.labelWater, "text-halo-color": p.halo }),
  },
  {
    test: startsWith("highway-name"),
    paint: (p) => ({ "text-color": p.labelRoad, "text-halo-color": p.halo, "text-halo-width": 1 }),
  },
  {
    test: (id) => id.startsWith("highway-shield") || id.startsWith("road_shield"),
    layout: { visibility: "none" },
  },
  {
    test: is("airport", "label_other", "label_state"),
    paint: (p) => ({ "text-color": p.labelMuted, "text-halo-color": p.halo }),
  },
  {
    test: startsWith("label_"),
    paint: (p) => ({
      "text-color": p.label,
      "text-halo-color": p.halo,
      "text-halo-width": 1.2,
      "icon-opacity": 0,
    }),
  },
];

/** Rewrite every colour literal inside a paint value (plain or expression) with `map`. */
export function mapColors(value: unknown, map: (color: Rgba) => Rgba): unknown {
  if (typeof value === "string") {
    const parsed = parseColor(value);
    return parsed ? formatRgba(map(parsed)) : value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => mapColors(item, map));
  }
  return value;
}

function isColorProperty(name: string): boolean {
  return name.endsWith("-color");
}

function restyleLayer(layer: LayerSpecification, palette: BasemapPalette): LayerSpecification {
  const rule = RULES.find((r) => r.test(layer.id));
  const next = structuredClone(layer) as LayerSpecification & { paint?: Paint; layout?: Paint };
  if (rule) {
    if (rule.paint) {
      next.paint = { ...(next.paint ?? {}), ...rule.paint(palette) };
    }
    if (rule.layout) {
      next.layout = { ...(next.layout ?? {}), ...rule.layout };
    }
    return next;
  }
  const fallback = palette.fallback;
  if (next.paint && fallback) {
    const paint: Paint = {};
    for (const [key, value] of Object.entries(next.paint)) {
      paint[key] = isColorProperty(key) ? mapColors(value, fallback) : value;
    }
    next.paint = paint;
  }
  return next;
}

/** A themed copy of `base`. */
export function restyle(base: StyleSpecification, theme: Theme): StyleSpecification {
  const palette = PALETTES[theme];
  return { ...base, layers: base.layers.map((layer) => restyleLayer(layer, palette)) };
}

/** Minimal stand-in when the tile host is unreachable: data layers still render on plain land. */
export function offlineStyle(theme: Theme): StyleSpecification {
  return {
    version: 8,
    sources: {},
    layers: [
      { id: "background", type: "background", paint: { "background-color": PALETTES[theme].land } },
    ],
  };
}

type SetPaint = Parameters<MapLibreMap["setPaintProperty"]>;
type SetLayout = Parameters<MapLibreMap["setLayoutProperty"]>;

export type PaintChange =
  | { layer: string; kind: "paint"; property: SetPaint[1]; value: SetPaint[2] }
  | { layer: string; kind: "layout"; property: SetLayout[1]; value: SetLayout[2] };

/**
 * Property changes that turn a map showing `current` into `target` (same layer stack, different
 * theme). Applied with `setPaintProperty` / `setLayoutProperty`, this re-themes in place.
 */
export function themeChanges(current: StyleSpecification, target: StyleSpecification) {
  const changes: PaintChange[] = [];
  const byId = new Map(current.layers.map((layer) => [layer.id, layer]));
  for (const layer of target.layers) {
    const before = byId.get(layer.id) as { paint?: Paint; layout?: Paint } | undefined;
    if (!before) continue;
    const after = layer as { paint?: Paint; layout?: Paint };
    for (const kind of ["paint", "layout"] as const) {
      const from = before[kind] ?? {};
      const to = after[kind] ?? {};
      for (const property of new Set([...Object.keys(from), ...Object.keys(to)])) {
        if (JSON.stringify(from[property]) !== JSON.stringify(to[property])) {
          changes.push({ layer: layer.id, kind, property, value: to[property] } as PaintChange);
        }
      }
    }
  }
  return changes;
}
