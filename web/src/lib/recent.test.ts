import { describe, expect, it } from "vitest";
import { RecentSet } from "./recent";

describe("recent set", () => {
  it("tells new values from ones it remembers", () => {
    const seen = new RecentSet<string>(3);
    expect(seen.add("a")).toBe(true);
    expect(seen.add("a")).toBe(false);
    expect(seen.has("a")).toBe(true);
    expect(seen.has("b")).toBe(false);
  });

  it("forgets the oldest values beyond its capacity", () => {
    const seen = new RecentSet<number>(3);
    for (const value of [1, 2, 3, 4]) seen.add(value);
    expect(seen.size).toBe(3);
    expect(seen.has(1)).toBe(false);
    expect([2, 3, 4].every((value) => seen.has(value))).toBe(true);
    // Remembering again does not make a value younger: 2 is still the oldest.
    expect(seen.add(2)).toBe(false);
    seen.add(5);
    expect(seen.has(2)).toBe(false);
    expect(seen.has(3)).toBe(true);
  });

  it("refuses an empty capacity", () => {
    expect(() => new RecentSet(0)).toThrow();
  });
});
