import { afterEach, describe, expect, it, vi } from "vitest";
import { notify, stackCards, useNotices } from "./notices";

afterEach(() => {
  useNotices.setState({ notices: [] });
  vi.useRealTimers();
});

describe("notices", () => {
  it("gives notices with an action more time than plain ones", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const plain = notify({ tone: "info", title: "Saved" });
    const actionable = notify({
      tone: "warning",
      title: "Zone changed in another session",
      action: { label: "Apply mine", run: () => {} },
    });
    const byId = new Map(useNotices.getState().notices.map((n) => [n.id, n.expiresAt]));
    expect(byId.get(plain)).toBe(1_005_000);
    expect(byId.get(actionable)).toBe(1_009_000);
  });

  it("keeps a held notice past its original expiry and drops it once released", () => {
    const id = notify({ tone: "warning", title: "Conflict", durationMs: 1_000 });
    const start = Date.now();
    useNotices.getState().setExpiry(id, start + 3_600_000);
    useNotices.getState().expire(start + 60_000);
    expect(useNotices.getState().notices.map((n) => n.id)).toEqual([id]);
    useNotices.getState().setExpiry(id, start + 62_500);
    useNotices.getState().expire(start + 63_000);
    expect(useNotices.getState().notices).toEqual([]);
  });

  it("keeps the three newest notices", () => {
    for (let i = 0; i < 5; i++) notify({ tone: "info", title: `n${i}` });
    expect(useNotices.getState().notices.map((n) => n.title)).toEqual(["n2", "n3", "n4"]);
  });
});

describe("stacking notices and alert toasts", () => {
  const toasts = ["t1", "t2", "t3", "t4", "t5"];

  it("fills every slot with the newest toasts when there is no notice", () => {
    expect(stackCards([], toasts, 3)).toEqual({ notices: [], toasts: ["t3", "t4", "t5"] });
  });

  it("never lets a burst of alerts push a notice out of view", () => {
    expect(stackCards(["conflict"], toasts, 3)).toEqual({
      notices: ["conflict"],
      toasts: ["t4", "t5"],
    });
    expect(stackCards(["a", "b", "c"], toasts, 3)).toEqual({
      notices: ["a", "b", "c"],
      toasts: [],
    });
  });

  it("gives the single phone slot to a notice first", () => {
    expect(stackCards(["conflict"], toasts, 1)).toEqual({ notices: ["conflict"], toasts: [] });
    expect(stackCards([], toasts, 1)).toEqual({ notices: [], toasts: ["t5"] });
  });

  it("shows nothing when there is no room", () => {
    expect(stackCards(["conflict"], toasts, 0)).toEqual({ notices: [], toasts: [] });
  });
});
