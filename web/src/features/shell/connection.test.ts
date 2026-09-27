import { describe, expect, it } from "vitest";
import { present } from "./connection";

const NOW = 1_000_000;

describe("connection presentation", () => {
  it("says Live, or Resumed for a few seconds after catching up", () => {
    expect(present({ state: "open", since: NOW - 60_000, resume: "fresh" }, NOW)).toMatchObject({
      tone: "live",
      label: "Live",
      canRetry: false,
    });
    expect(present({ state: "open", since: NOW - 1_000, resume: "replay" }, NOW).label).toBe(
      "Resumed",
    );
    expect(present({ state: "open", since: NOW - 9_000, resume: "replay" }, NOW).label).toBe(
      "Live",
    );
  });

  it("counts down to the next attempt and offers to retry now", () => {
    const waiting = present(
      { state: "waiting", attempt: 3, retryAt: NOW + 4_200, lastCode: 1006 },
      NOW,
    );
    expect(waiting).toMatchObject({ tone: "warn", label: "Retrying in 5s", canRetry: true });
    expect(
      present({ state: "waiting", attempt: 1, retryAt: NOW + 2_000, lastCode: 1013 }, NOW).detail,
    ).toMatch(/busy/);
  });

  it("explains the states that need the user", () => {
    expect(present({ state: "blocked", code: 4009, reason: "" }, NOW)).toMatchObject({
      label: "Too many sessions",
      canRetry: true,
    });
    expect(present({ state: "blocked", code: 4003, reason: "" }, NOW).label).toBe("Not allowed");
    expect(present({ state: "offline" }, NOW)).toMatchObject({ tone: "down", label: "Offline" });
    expect(present({ state: "connecting", attempt: 0 }, NOW).label).toBe("Connecting");
    expect(present({ state: "connecting", attempt: 2 }, NOW).label).toBe("Reconnecting");
    expect(present({ state: "paused" }, NOW).canRetry).toBe(true);
  });
});
