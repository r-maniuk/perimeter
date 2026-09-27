// @vitest-environment jsdom
import "@/test/dom";
import { act, cleanup, render, screen } from "@testing-library/react";
import { Tooltip } from "radix-ui";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OpsFrame } from "@/api/schemas";
import { useLive } from "@/state/live";
import { seriesOf } from "./model";
import { OpsPanel } from "./OpsPanel";

/** This browser's clock runs 45 s ahead of the servers'. */
const SKEW_MS = 45_000;
const runtime = vi.hoisted(() => ({
  live: { clock: { now: () => Date.now() - 45_000, offsetMs: -45_000 } },
}));

vi.mock("@/app/runtime", () => ({ getRuntime: () => runtime }));

afterEach(() => {
  cleanup();
  useLive.getState().reset();
});

describe("pipeline panel", () => {
  it("measures heartbeat ages on the servers' clock, not this browser's", () => {
    const serverS = (Date.now() - SKEW_MS) / 1000;
    const frame: OpsFrame = {
      type: "ops",
      ts: serverS,
      services: [
        { service: "api", instance: "api-1", ts: serverS - 0.4, loop_lag_p99_ms: 1.2 },
        { service: "engine", instance: "engine-1", ts: serverS - 7, loop_lag_p99_ms: 2.5 },
      ],
    };
    useLive.getState().pushOps(frame, seriesOf(frame));
    render(
      <Tooltip.Provider>
        <OpsPanel onClose={() => {}} titleId="ops-title" />
      </Tooltip.Provider>,
    );
    const ages = screen.getAllByTitle("Time since the last heartbeat");
    expect(ages.map((age) => age.textContent)).toEqual(["now", "7 s"]);
  });

  it("counts one of anything in the singular", () => {
    const serverS = (Date.now() - SKEW_MS) / 1000;
    const pipeline: OpsFrame = {
      type: "ops",
      ts: serverS,
      services: [
        { service: "api", instance: "api-1", ts: serverS, lag: 1, sessions: 1 },
        { service: "engine", instance: "engine-1", ts: serverS, relay_backlog: 1, partitions: [3] },
      ],
    };
    useLive.getState().pushOps(pipeline, seriesOf(pipeline));
    const view = render(
      <Tooltip.Provider>
        <OpsPanel onClose={() => {}} titleId="ops-title" />
      </Tooltip.Provider>,
    );
    const text = () => view.container.textContent ?? "";
    expect(text()).toContain("Live from 2 processes");
    expect(text()).toContain("Stream backlog1 msg");
    expect(text()).toContain("Outbox backlog1 row");
    expect(text()).toContain("1 session · db pool 0");
    expect(text()).toContain("1 partition · 0.0 batches/s");
    expect(text()).toMatch(/engine-11 partition(?!s)/);

    // One process left reporting.
    const alone: OpsFrame = { ...pipeline, services: pipeline.services.slice(0, 1) };
    act(() => useLive.getState().pushOps(alone, seriesOf(alone)));
    expect(text()).toContain("Live from 1 process ·");
  });
});
