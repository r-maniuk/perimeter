/** How the connection state is presented: one short label, a sentence of detail, a tone. */
import type { LiveStatus } from "@/live/client";

export type Tone = "live" | "busy" | "warn" | "down";

export interface Presentation {
  tone: Tone;
  label: string;
  detail: string;
  canRetry: boolean;
}

const RESUMED_FOR_MS = 5_000;

export function present(status: LiveStatus, now: number): Presentation {
  switch (status.state) {
    case "open": {
      const resumed = status.resume === "replay" && now - status.since < RESUMED_FOR_MS;
      return {
        tone: "live",
        label: resumed ? "Resumed" : "Live",
        detail: resumed
          ? "Reconnected and caught up on every event you missed."
          : "Streaming positions and events in real time.",
        canRetry: false,
      };
    }
    case "connecting":
      return {
        tone: "busy",
        label: status.attempt > 0 ? "Reconnecting" : "Connecting",
        detail: "Opening the live channel…",
        canRetry: false,
      };
    case "waiting": {
      const seconds = Math.max(0, Math.ceil((status.retryAt - now) / 1000));
      return {
        tone: "warn",
        label: seconds > 0 ? `Retrying in ${seconds}s` : "Reconnecting",
        detail:
          status.lastCode === 1013
            ? "The server is busy; backing off before reconnecting."
            : "Connection lost. Nothing is missed: events resume from where you left off.",
        canRetry: true,
      };
    }
    case "offline":
      return {
        tone: "down",
        label: "Offline",
        detail: "Waiting for the network to come back.",
        canRetry: false,
      };
    case "paused":
      return {
        tone: "busy",
        label: "Paused",
        detail: "Paused while this tab was in the background.",
        canRetry: true,
      };
    case "blocked":
      return {
        tone: "down",
        label: status.code === 4009 ? "Too many sessions" : "Not allowed",
        detail:
          status.code === 4009
            ? "Sign out another session to connect this one."
            : "The server refused this connection.",
        canRetry: true,
      };
    case "signedOut":
      return { tone: "down", label: "Signed out", detail: "", canRetry: false };
    default:
      return { tone: "busy", label: "Starting", detail: "", canRetry: false };
  }
}
