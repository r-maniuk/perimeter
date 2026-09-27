import { describe, expect, it } from "vitest";
import { mercatorX } from "@/lib/geodesy";
import { worldCopies } from "./deviceLayer";

/** Mercator x range of a city-wide view centred on longitude `lon` (may be unwrapped). */
function cityView(lon: number): [number, number, number] {
  return [mercatorX(lon - 0.1), mercatorX(lon + 0.1), mercatorX(lon)];
}

describe("worldCopies", () => {
  const origin = mercatorX(4.89);

  it("draws one copy, the camera's, at city zoom", () => {
    expect(worldCopies(origin, ...cityView(4.89))).toEqual([0]);
    expect(worldCopies(origin, ...cityView(364.89))).toEqual([1]);
    expect(worldCopies(origin, ...cityView(-715.11))).toEqual([-2]);
  });

  it("draws every copy a zoomed-out view shows, nearest to the camera first", () => {
    // About 2.8 worlds across a wide screen at zoom 0.
    expect(worldCopies(0.5, -0.9, 1.9, 0.5)).toEqual([0, -1, 1]);
    expect(worldCopies(0.5, 0.1, 2.9, 1.5)).toEqual([1, 0, 2]);
  });

  it("keeps a view straddling the antimeridian to one copy around the origin", () => {
    const nearSeam = mercatorX(179.9);
    expect(worldCopies(nearSeam, ...cityView(179.95))).toEqual([0]);
    expect(worldCopies(nearSeam, ...cityView(180.05))).toEqual([0]);
  });

  it("caps the number of copies, keeping those nearest the camera", () => {
    const copies = worldCopies(0.5, -20, 20, 0.5);
    expect(copies).toHaveLength(6);
    expect(copies.slice(0, 3)).toEqual([0, -1, 1]);
  });
});
