/** Realtime side-channel state: connection health, sessions, ops metrics, fleet counters. */
import { create } from "zustand";
import type { OpsFrame } from "@/api/schemas";
import type { LiveStatus } from "@/live/client";

/** One minute of one-second samples per metric series. */
export const HISTORY = 60;

export interface OpsSample {
  at: number;
  frame: OpsFrame;
}

interface LiveState {
  status: LiveStatus;
  latencyMs: number | null;
  sessionId: string | null;
  replica: string | null;
  /** Events replayed after a reconnect, shown as "N events while you were away". */
  away: { count: number; at: number } | null;
  ops: OpsSample | null;
  history: Record<string, number[]>;
  devicesInView: number;
  movingInView: number;
  /** Devices that reported from inside each zone during the last few seconds. */
  reporting: Record<string, number>;
  setStatus(status: LiveStatus): void;
  setLatency(ms: number): void;
  setHello(sessionId: string, replica: string | null): void;
  addAway(count: number): void;
  clearAway(): void;
  pushOps(frame: OpsFrame, series: Record<string, number>): void;
  setFleet(devices: number, moving: number): void;
  setReporting(reporting: Record<string, number>): void;
  reset(): void;
}

const initial = {
  status: { state: "idle" } as LiveStatus,
  latencyMs: null,
  sessionId: null,
  replica: null,
  away: null,
  ops: null,
  history: {},
  devicesInView: 0,
  movingInView: 0,
  reporting: {},
};

export const useLive = create<LiveState>()((set) => ({
  ...initial,
  setStatus: (status) => set({ status }),
  setLatency: (latencyMs) => set({ latencyMs }),
  setHello: (sessionId, replica) => set({ sessionId, replica }),
  addAway: (count) =>
    set((s) => ({ away: { count: (s.away?.count ?? 0) + count, at: Date.now() } })),
  clearAway: () => set({ away: null }),
  pushOps: (frame, series) =>
    set((s) => {
      const history: Record<string, number[]> = {};
      for (const [key, value] of Object.entries(series)) {
        const previous = s.history[key] ?? [];
        history[key] = [...previous.slice(-(HISTORY - 1)), value];
      }
      return { ops: { at: Date.now(), frame }, history };
    }),
  setFleet: (devicesInView, movingInView) => set({ devicesInView, movingInView }),
  setReporting: (reporting) => set({ reporting }),
  reset: () => set(initial),
}));
