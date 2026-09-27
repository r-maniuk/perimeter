import { describe, expect, it } from "vitest";
import {
  compassPoint,
  formatAge,
  formatCoordinate,
  formatCount,
  formatDay,
  formatDistance,
  formatDuration,
  formatHeading,
  formatMs,
  formatRate,
  formatSpeed,
  parseCoordinatePair,
  parseDegrees,
  parseDistance,
  plural,
} from "./format";

describe("format", () => {
  it("counts and rates", () => {
    expect(formatCount(9_812)).toBe("9,812");
    expect(formatCount(12_400)).toBe("12.4K");
    expect(formatRate(3_333.3)).toBe("3.3K/s");
    expect(formatRate(12.4)).toBe("12/s");
    expect(formatRate(0.44)).toBe("0.4/s");
    expect(formatRate(Number.NaN)).toBe("–");
  });

  it("names what is counted in the singular only for a count shown as one", () => {
    expect(`${formatCount(1)} ${plural(1, "row")}`).toBe("1 row");
    expect(plural(0, "row")).toBe("rows");
    expect(plural(3, "row")).toBe("rows");
    expect(plural(1, "process", "processes")).toBe("process");
    expect(plural(2, "process", "processes")).toBe("processes");
    // The noun follows the number on screen: 0.6 is shown as "1", 1.5 as "2", 12,400 as "12.4K".
    expect(`${formatCount(0.6)} ${plural(0.6, "row")}`).toBe("1 row");
    expect(`${formatCount(1.5)} ${plural(1.5, "row")}`).toBe("2 rows");
    expect(`${formatCount(12_400)} ${plural(12_400, "report")}`).toBe("12.4K reports");
  });

  it("durations and latencies", () => {
    expect(formatMs(3.21)).toBe("3.2 ms");
    expect(formatMs(0)).toBe("<1 ms");
    expect(formatMs(412)).toBe("412 ms");
    expect(formatMs(42.04)).toBe("42 ms");
    expect(formatMs(12_345)).toBe("12.3 s");
    expect(formatMs(null)).toBe("–");
    expect(formatDuration(45)).toBe("45 s");
    expect(formatDuration(300)).toBe("5 min");
    expect(formatDuration(90)).toBe("1.5 min");
    expect(formatDuration(5_400)).toBe("1.5 h");
  });

  it("distances, speeds and headings", () => {
    expect(formatDistance(850.4)).toBe("850 m");
    expect(formatDistance(1_240)).toBe("1.24 km");
    expect(formatDistance(12_500)).toBe("12.5 km");
    expect(formatDistance(100_000)).toBe("100 km");
    expect(formatSpeed(12.5)).toBe("45 km/h");
    expect(formatSpeed(1.2)).toBe("4.3 km/h");
    expect(formatSpeed(null)).toBe("–");
    expect(compassPoint(44)).toBe("NE");
    expect(compassPoint(-10)).toBe("N");
    expect(formatHeading(181.5)).toBe("S 182°");
  });

  it("relative times and days", () => {
    expect(formatAge(1_000)).toBe("now");
    expect(formatAge(8_200)).toBe("8 s ago");
    expect(formatAge(185_000)).toBe("3 min ago");
    expect(formatAge(7_300_000)).toBe("2 h ago");
    const now = new Date(2026, 8, 26, 12).getTime();
    expect(formatDay(now - 3_600_000, now)).toBe("Today");
    expect(formatDay(now - 86_400_000, now)).toBe("Yesterday");
    const older = now - 5 * 86_400_000;
    // Month abbreviations vary between ICU releases ("Sep"/"Sept"), so compare with the platform's.
    const expected = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" }).format(
      older,
    );
    expect(formatDay(older, now)).toBe(expected);
    expect(expected.startsWith("21 ")).toBe(true);
  });

  it("coordinates", () => {
    expect(formatCoordinate(52.3676, 4.9041)).toBe("52.36760° N  4.90410° E");
    expect(formatCoordinate(-33.8688, -151.2093)).toBe("33.86880° S  151.20930° W");
  });
});

describe("parseDistance", () => {
  it.each([
    ["850", 850],
    ["850 m", 850],
    ["1.2 km", 1_200],
    ["1,5km", 1_500],
    ["2k", 2_000],
    [" 40 M ", 40],
  ])("reads %j", (input, expected) => {
    expect(parseDistance(input)).toBe(expected);
  });

  it.each(["", "abc", "-5", "1.2.3 km", "5 mi"])("rejects %j", (input) => {
    expect(parseDistance(input)).toBeNull();
  });
});

describe("typed coordinates", () => {
  it.each([
    ["52.3731", "lat", 52.3731],
    ["52,3731", "lat", 52.3731],
    [" -33.8688 ", "lat", -33.8688],
    ["52.3731° N", "lat", 52.3731],
    ["33.8688 s", "lat", -33.8688],
    ["4.8926E", "lon", 4.8926],
    ["74.006° W", "lon", -74.006],
    ["+180", "lon", 180],
    ["-90", "lat", -90],
  ] as const)("reads %j as a %s of %d", (input, axis, value) => {
    expect(parseDegrees(input, axis)).toBeCloseTo(value, 9);
  });

  it.each([
    ["", "lat"],
    ["north", "lat"],
    ["90.5", "lat"],
    ["-181", "lon"],
    ["4.9 N", "lon"],
    ["52.3 E", "lat"],
    ["-4.9 W", "lon"],
    ["52.3731, 4.8926", "lat"],
  ] as const)("refuses %j as a %s", (input, axis) => {
    expect(parseDegrees(input, axis)).toBeNull();
  });

  it.each([
    ["52.3731, 4.8926", { lat: 52.3731, lon: 4.8926 }],
    ["52.3731 4.8926", { lat: 52.3731, lon: 4.8926 }],
    ["52.373100;-4.892600", { lat: 52.3731, lon: -4.8926 }],
    ["52.37310° N  4.89260° E", { lat: 52.3731, lon: 4.8926 }],
    ["40.7128 N, 74.0060 W", { lat: 40.7128, lon: -74.006 }],
  ])("reads the pair %j", (input, pair) => {
    const parsed = parseCoordinatePair(input);
    expect(parsed?.lat).toBeCloseTo(pair.lat, 9);
    expect(parsed?.lon).toBeCloseTo(pair.lon, 9);
  });

  it.each(["52.3731", "52,3731, 4,8926", "95, 4", "52.3 E, 4.8 N", "1, 2, 3"])(
    "refuses the pair %j",
    (input) => {
      expect(parseCoordinatePair(input)).toBeNull();
    },
  );
});
