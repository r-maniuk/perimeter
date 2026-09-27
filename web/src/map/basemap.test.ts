import type { StyleSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";
import { contrast, hexToRgba, parseColor } from "@/lib/color";
import { DARK_PALETTE, LIGHT_PALETTE, mapColors, restyle, themeChanges } from "./basemap";

/** A slice of the upstream layer stack, including a layer no rule knows about. */
const base: StyleSpecification = {
  version: 8,
  sources: { openmaptiles: { type: "vector", url: "https://tiles.example/planet" } },
  layers: [
    { id: "background", type: "background", paint: { "background-color": "rgb(242,243,240)" } },
    {
      id: "water",
      type: "fill",
      source: "openmaptiles",
      "source-layer": "water",
      paint: { "fill-color": "rgb(194, 200, 202)", "fill-antialias": true },
    },
    {
      id: "highway_motorway_inner",
      type: "line",
      source: "openmaptiles",
      "source-layer": "transportation",
      paint: {
        "line-color": ["interpolate", ["linear"], ["zoom"], 5.8, "hsla(0,0%,85%,0.53)", 6, "#fff"],
        "line-width": ["interpolate", ["exponential", 1.4], ["zoom"], 4, 2, 6, 1.3, 20, 30],
      },
    },
    {
      id: "highway-shield-non-us",
      type: "symbol",
      source: "openmaptiles",
      "source-layer": "transportation_name",
      layout: { "text-field": "{ref}" },
    },
    {
      id: "label_city",
      type: "symbol",
      source: "openmaptiles",
      "source-layer": "place",
      paint: { "text-color": "#000", "text-halo-color": "#fff" },
    },
    {
      id: "future_layer",
      type: "fill",
      source: "openmaptiles",
      "source-layer": "landuse",
      paint: {
        "fill-color": ["match", ["get", "class"], "school", "#f0e8f8", "rgb(230, 230, 230)"],
      },
    },
  ],
};

function paintOf(style: StyleSpecification, id: string): Record<string, unknown> {
  const layer = style.layers.find((l) => l.id === id) as { paint?: Record<string, unknown> };
  return layer.paint ?? {};
}

describe("restyle", () => {
  it("repaints known layer families from the palette", () => {
    const light = restyle(base, "light");
    expect(paintOf(light, "background")["background-color"]).toBe(LIGHT_PALETTE.land);
    expect(paintOf(light, "water")["fill-color"]).toBe(LIGHT_PALETTE.water);
    expect(paintOf(light, "water")["fill-antialias"]).toBe(true);
    expect(paintOf(light, "highway_motorway_inner")["line-color"]).toBe(LIGHT_PALETTE.road);
    expect(paintOf(light, "label_city")["text-color"]).toBe(LIGHT_PALETTE.label);
  });

  it("keeps zoom curves and hides road shields", () => {
    const dark = restyle(base, "dark");
    expect(paintOf(dark, "highway_motorway_inner")["line-width"]).toEqual(
      paintOf(base, "highway_motorway_inner")["line-width"],
    );
    const shield = dark.layers.find((l) => l.id === "highway-shield-non-us") as {
      layout?: Record<string, unknown>;
    };
    expect(shield.layout?.visibility).toBe("none");
    expect(shield.layout?.["text-field"]).toBe("{ref}");
  });

  it("maps unknown layers through the theme's fallback, inside expressions too", () => {
    const dark = restyle(base, "dark");
    const expression = paintOf(dark, "future_layer")["fill-color"] as unknown[];
    expect(expression.slice(0, 3)).toEqual(["match", ["get", "class"], "school"]);
    const school = parseColor(expression[3] as string);
    const other = parseColor(expression[4] as string);
    expect(school && other).toBeTruthy();
    // Light colours become dark ones.
    expect(school && contrast(school, hexToRgba("#000000"))).toBeLessThan(3);
    expect(restyle(base, "light").layers.at(-1)).toEqual(base.layers.at(-1));
  });

  it("does not mutate the base style", () => {
    const snapshot = JSON.stringify(base);
    restyle(base, "dark");
    expect(JSON.stringify(base)).toBe(snapshot);
  });

  it("gives labels readable contrast in both themes", () => {
    for (const palette of [LIGHT_PALETTE, DARK_PALETTE]) {
      const land = hexToRgba(palette.land);
      expect(contrast(hexToRgba(palette.label), land)).toBeGreaterThan(7);
      expect(contrast(hexToRgba(palette.labelMuted), land)).toBeGreaterThan(4.5);
    }
  });
});

describe("theme switching in place", () => {
  it("lists exactly the properties that differ between themes", () => {
    const light = restyle(base, "light");
    const dark = restyle(base, "dark");
    const changes = themeChanges(light, dark);
    expect(changes).toContainEqual({
      layer: "water",
      kind: "paint",
      property: "fill-color",
      value: DARK_PALETTE.water,
    });
    expect(changes.some((c) => c.property === "line-width")).toBe(false);
    expect(themeChanges(dark, dark)).toEqual([]);
  });

  it("leaves non-colour strings in expressions untouched", () => {
    expect(mapColors(["get", "class"], () => ({ r: 0, g: 0, b: 0, a: 1 }))).toEqual([
      "get",
      "class",
    ]);
  });
});
