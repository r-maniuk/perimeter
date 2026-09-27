// @vitest-environment jsdom
import "@/test/dom";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLive } from "@/state/live";
import { ConnectionPill } from "./ConnectionPill";

vi.mock("@/app/runtime", () => ({ getRuntime: () => null }));

afterEach(() => {
  cleanup();
  useLive.getState().reset();
});

describe("connection popover", () => {
  it("keeps the last event and the clock offset current while it is open", async () => {
    useLive.getState().setStatus({ state: "open", since: Date.now() - 60_000, resume: "fresh" });
    useLive.getState().setHello({
      sessionId: "s-1",
      replica: "api-1",
      lastSeq: 40,
      clockOffsetMs: 0.4,
    });
    render(<ConnectionPill />);
    await userEvent.click(screen.getByRole("button", { name: "Connection: Live" }));
    const facts = await screen.findByRole("dialog");
    expect(within(facts).getByText("#40")).toBeTruthy();
    expect(within(facts).getByText("<1 ms")).toBeTruthy();

    act(() => useLive.getState().setLastSeq(41));
    act(() => useLive.getState().setLatency(12, -1_500));
    expect(within(facts).getByText("#41")).toBeTruthy();
    expect(within(facts).getByText("−1,500 ms")).toBeTruthy();
    expect(within(facts).getByText("12 ms")).toBeTruthy();
  });
});
