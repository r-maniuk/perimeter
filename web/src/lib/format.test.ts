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
  parseDistance,
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
