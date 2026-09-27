import { describe, expect, it } from "vitest";
import type { LiveSession } from "@/api/schemas";
import { connectedAt, groupSessions } from "./model";

function session(sid: string, minute: number, current: boolean): LiveSession {
  return {
    sid,
    label: "Chrome · macOS",
    agent: "Mozilla/5.0",
    ip: "10.0.0.7",
    replica: "api-1",
    connected_at: `2026-09-27T10:${String(minute).padStart(2, "0")}:00Z`,
    current,
  };
}

describe("groupSessions", () => {
  const list = [
    session("phone", 30, false),
    session("tab-old", 5, true),
    session("me", 10, true),
    session("laptop", 45, false),
    session("tab-new", 50, true),
  ];

  it("puts this tab first, then the other tabs of this sign-in, newest first", () => {
    const { here } = groupSessions(list, "me");
    expect(here.map((r) => [r.session.sid, r.thisTab])).toEqual([
      ["me", true],
      ["tab-new", false],
      ["tab-old", false],
    ]);
  });

  it("offers only other sign-ins for remote sign-out, newest first", () => {
    const { elsewhere } = groupSessions(list, "me");
    expect(elsewhere.map((s) => s.sid)).toEqual(["laptop", "phone"]);
  });

  it("keeps this sign-in together before this tab knows its own socket", () => {
    const { here, elsewhere } = groupSessions(list, null);
    expect(here.map((r) => [r.session.sid, r.thisTab])).toEqual([
      ["tab-new", null],
      ["me", null],
      ["tab-old", null],
    ]);
    expect(elsewhere).toHaveLength(2);
  });

  it("does not reorder the list it was given", () => {
    const copy = [...list];
    groupSessions(list, "me");
    expect(list).toEqual(copy);
  });

  it("reads connection times, and survives an unreadable one", () => {
    expect(connectedAt(session("x", 7, false))).toBe(Date.parse("2026-09-27T10:07:00Z"));
    expect(connectedAt({ ...session("x", 7, false), connected_at: "soon" })).toBeNull();
  });
});
