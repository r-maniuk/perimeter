/** Small helpers around MapLibre sources. */
import type { GeoJSONSource } from "maplibre-gl";

/**
 * Replace a GeoJSON source's data. MapLibre resolves the returned promise once the worker has
 * re-tiled the data; a newer `setData` supersedes an older one, whose promise then rejects with an
 * AbortError. That is expected, anything else is reported.
 */
export function setSourceData(source: GeoJSONSource, data: GeoJSON.GeoJSON): void {
  source.setData(data).catch((error: unknown) => {
    if (error instanceof Error && error.name === "AbortError") return;
    console.error("map source update failed", error);
  });
}
