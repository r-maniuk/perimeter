/**
 * The realtime channel (`/v1/live`, spec §12): one WebSocket per tab carrying binary position
 * bundles and JSON frames.
 *
 * Responsibilities kept here, independent of React and testable with a fake socket:
 * - connection lifecycle with exponential back-off and jitter, honouring the close codes
 *   (4001 signed out, 4003 forbidden, 4008 resume now, 4009 too many sessions, 1013 overloaded);
 * - exactly-once events: `seq`/`prev` sequencing, `last_seq` persisted per tab, `resume_after` on
 *   every reconnect, a client-side gap check that reconnects to heal;
 * - liveness: application ping/pong with a deadline (half-open sockets are detected and replaced),
 *   latency and server clock offset;
 * - re-sending the viewport and the ops subscription after every (re)connect and on `resync`;
 * - resource hygiene: the socket is released while the tab stays hidden and resumed on return,
 *   and reconnection waits for the browser to report connectivity.
 */
import * as v from "valibot";
import {
  type EventEnvelope,
  EventEnvelopeSchema,
  type HelloFrame,
  type LiveSession,
  type OpsFrame,
  type PulseFrame,
  ServerFrameSchema,
} from "@/api/schemas";
import { foldBBox } from "@/lib/tiles";
import { backoffDelay, DEFAULT_BACKOFF } from "./backoff";
import { ClockSync } from "./clock";
import { EventSequencer } from "./sequencer";

export const CloseCode = {
  Normal: 1000,
  GoingAway: 1001,
  Internal: 1011,
  Overloaded: 1013,
  SignedOut: 4001,
  Forbidden: 4003,
  EventOverflow: 4008,
  TooManySessions: 4009,
} as const;

/** Close codes the client uses itself (never sent by the server). */
const LOCAL_GAP = 4900;
const LOCAL_DEAD = 4901;
const LOCAL_NO_HELLO = 4902;
const IMMEDIATE_RESUME_SPACING_MS = 5_000;

export interface SocketLike {
  binaryType: BinaryType;
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface Viewport {
  bbox: [number, number, number, number];
  zoom: number;
}

export type LiveStatus =
  | { state: "idle" }
  | { state: "connecting"; attempt: number }
  | { state: "open"; since: number; resume: HelloFrame["resume"]["mode"] }
  | { state: "waiting"; attempt: number; retryAt: number; lastCode: number | null }
  | { state: "offline" }
  | { state: "paused" }
  | { state: "blocked"; code: number; reason: string }
  | { state: "signedOut" }
  | { state: "stopped" };

export type LiveEvent =
  | { type: "status"; status: LiveStatus }
  | { type: "hello"; hello: HelloFrame }
  | { type: "positions"; data: ArrayBuffer }
  | { type: "event"; seq: number; event: EventEnvelope; replayed: boolean }
  | { type: "reset" }
  | { type: "pulse"; frame: PulseFrame }
  | { type: "sessions"; sessions: LiveSession[] }
  | { type: "resync" }
  | { type: "ops"; frame: OpsFrame }
  | { type: "latency"; rttMs: number }
  | { type: "protocolError"; message: string };

export interface SeqStore {
  get(): number | null;
  set(seq: number | null): void;
}

export interface Environment {
  online(): boolean;
  hidden(): boolean;
  onConnectivity(listener: (online: boolean) => void): () => void;
  onVisibility(listener: (hidden: boolean) => void): () => void;
}

export interface LiveClientOptions {
  /** Absolute `ws(s)://…/v1/live` URL, without query. */
  url: string;
  seqStore: SeqStore;
  createSocket?: (url: string) => SocketLike;
  environment?: Environment;
  random?: () => number;
  pingIntervalMs?: number;
  pongTimeoutMs?: number;
  /** How long a hidden tab keeps its socket before releasing it. */
  hiddenGraceMs?: number;
  /** A connection that stayed up this long resets the back-off. */
  stableAfterMs?: number;
  /** A socket that does not say hello within this time is replaced. */
  helloTimeoutMs?: number;
}

const browserEnvironment: Environment = {
  online: () => navigator.onLine,
  hidden: () => document.visibilityState === "hidden",
  onConnectivity(listener) {
    const on = () => listener(true);
    const off = () => listener(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  },
  onVisibility(listener) {
    const handler = () => listener(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  },
};

/** `ws(s)://<this origin>/v1/live` */
export function liveUrl(location: Location = window.location): string {
  const url = new URL("/v1/live", location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export class LiveClient {
  readonly clock = new ClockSync();
  #options: Required<Omit<LiveClientOptions, "createSocket" | "environment">> & {
    createSocket: (url: string) => SocketLike;
    environment: Environment;
  };
  #listeners = new Set<(event: LiveEvent) => void>();
  #status: LiveStatus = { state: "idle" };
  #socket: SocketLike | null = null;
  #sequencer: EventSequencer;
  #attempt = 0;
  #lastCode: number | null = null;
  #viewport: Viewport | null = null;
  /** The viewport last sent on the current socket (re-sent only when it changed). */
  #sentViewport: Viewport | null = null;
  #ops = false;
  #hello: HelloFrame | null = null;
  #replayUntilMs = 0;
  #timers = new Set<ReturnType<typeof setTimeout>>();
  #retryTimer: ReturnType<typeof setTimeout> | null = null;
  #hiddenTimer: ReturnType<typeof setTimeout> | null = null;
  #pingTimer: ReturnType<typeof setInterval> | null = null;
  #pongDeadline: ReturnType<typeof setTimeout> | null = null;
  #stableTimer: ReturnType<typeof setTimeout> | null = null;
  #helloDeadline: ReturnType<typeof setTimeout> | null = null;
  #lastImmediateAt = Number.NEGATIVE_INFINITY;
  #unsubscribe: (() => void)[] = [];

  constructor(options: LiveClientOptions) {
    this.#options = {
      createSocket: (url) => new WebSocket(url),
      environment: browserEnvironment,
      random: Math.random,
      pingIntervalMs: 15_000,
      pongTimeoutMs: 10_000,
      hiddenGraceMs: 120_000,
      stableAfterMs: 10_000,
      helloTimeoutMs: 10_000,
      ...options,
    };
    this.#sequencer = new EventSequencer(options.seqStore.get());
  }

  get status(): LiveStatus {
    return this.#status;
  }

  get hello(): HelloFrame | null {
    return this.#hello;
  }

  get lastSeq(): number | null {
    return this.#sequencer.last;
  }

  subscribe(listener: (event: LiveEvent) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  start(): void {
    if (this.#status.state !== "idle" && this.#status.state !== "stopped") return;
    const env = this.#options.environment;
    this.#unsubscribe.push(
      env.onConnectivity((online) => this.#onConnectivity(online)),
      env.onVisibility((hidden) => this.#onVisibility(hidden)),
    );
    this.#connect();
  }

  /** Close for good (sign-out, unmount). */
  stop(): void {
    for (const off of this.#unsubscribe.splice(0)) off();
    this.#clearTimers();
    this.#dropSocket(CloseCode.Normal, "client stopped");
    this.#setStatus({ state: "stopped" });
  }

  /** Reconnect now instead of waiting out the back-off (user action, or after `blocked`). */
  retryNow(): void {
    const state = this.#status.state;
    if (state === "open" || state === "connecting" || state === "stopped") return;
    if (state === "signedOut") return;
    this.#attempt = 0;
    this.#connect();
  }

  setViewport(viewport: Viewport): void {
    this.#viewport = viewport;
    this.#sendViewport(viewport);
  }

  #sendViewport(viewport: Viewport): void {
    const socket = this.#socket;
    if (socket?.readyState !== 1) return;
    // The map reports unwrapped longitudes once panned around the world; the protocol wants them
    // within ±540°, so they go out folded into the first world (the covered tiles are the same).
    const [west, south, east, north] = viewport.bbox;
    const box = foldBBox({ west, south, east, north });
    const bbox = [box.west, box.south, box.east, box.north];
    socket.send(JSON.stringify({ type: "viewport", bbox, zoom: viewport.zoom }));
    this.#sentViewport = viewport;
  }

  setOps(on: boolean): void {
    if (this.#ops === on) return;
    this.#ops = on;
    this.#send({ type: "ops", on });
  }

  /* ---------------------------------------------------------------- connection */

  #connect(): void {
    if (this.#retryTimer) {
      clearTimeout(this.#retryTimer);
      this.#retryTimer = null;
    }
    if (!this.#options.environment.online()) {
      this.#setStatus({ state: "offline" });
      return;
    }
    this.#dropSocket(CloseCode.Normal, "reconnecting");
    const after = this.#sequencer.last;
    const url = after === null ? this.#options.url : `${this.#options.url}?resume_after=${after}`;
    let socket: SocketLike;
    try {
      socket = this.#options.createSocket(url);
    } catch (error) {
      this.#emit({ type: "protocolError", message: `cannot open socket: ${String(error)}` });
      this.#scheduleReconnect(null);
      return;
    }
    socket.binaryType = "arraybuffer";
    this.#socket = socket;
    this.#sentViewport = null;
    this.#setStatus({ state: "connecting", attempt: this.#attempt });
    socket.onopen = () => {
      // The viewport goes out as the very first message: the server then knows no resume request
      // is coming and starts streaming at once instead of waiting for one.
      if (this.#socket === socket && this.#viewport) this.#sendViewport(this.#viewport);
    };
    socket.onmessage = (event) => {
      if (this.#socket === socket) this.#onMessage(event.data);
    };
    socket.onclose = (event) => {
      if (this.#socket === socket) this.#onClose(event.code, event.reason);
    };
    socket.onerror = () => {
      // Errors are always followed by a close event, which carries the decision.
    };
    this.#helloDeadline = this.#timer(() => {
      this.#helloDeadline = null;
      if (this.#socket !== socket || this.#hello) return;
      this.#dropSocket(LOCAL_NO_HELLO, "no hello");
      this.#scheduleReconnect(LOCAL_NO_HELLO);
    }, this.#options.helloTimeoutMs);
  }

  #dropSocket(code: number, reason: string): void {
    const socket = this.#socket;
    this.#socket = null;
    this.#hello = null;
    this.#stopHeartbeat();
    if (this.#helloDeadline) clearTimeout(this.#helloDeadline);
    this.#helloDeadline = null;
    if (!socket) return;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    socket.onopen = null;
    try {
      socket.close(code, reason);
    } catch {
      // Already closed.
    }
  }

  #onClose(code: number, reason: string): void {
    this.#socket = null;
    this.#hello = null;
    this.#stopHeartbeat();
    if (this.#helloDeadline) clearTimeout(this.#helloDeadline);
    this.#helloDeadline = null;
    this.#lastCode = code;
    switch (code) {
      case CloseCode.SignedOut:
        this.#setStatus({ state: "signedOut" });
        return;
      case CloseCode.Forbidden:
        this.#setStatus({ state: "blocked", code, reason: reason || "forbidden" });
        return;
      case CloseCode.TooManySessions:
        this.#setStatus({ state: "blocked", code, reason: reason || "too many sessions" });
        return;
      case CloseCode.EventOverflow:
        // The session fell behind on events: resume from the last one seen, right away.
        this.#resumeNow(code);
        return;
      default:
        this.#scheduleReconnect(code);
    }
  }

  /** Reconnect immediately — unless that already happened moments ago (then back off). */
  #resumeNow(code: number): void {
    const now = Date.now();
    if (now - this.#lastImmediateAt < IMMEDIATE_RESUME_SPACING_MS) {
      this.#scheduleReconnect(code);
      return;
    }
    this.#lastImmediateAt = now;
    this.#connect();
  }

  #scheduleReconnect(code: number | null): void {
    if (!this.#options.environment.online()) {
      this.#setStatus({ state: "offline" });
      return;
    }
    const policy =
      code === CloseCode.Overloaded ? { ...DEFAULT_BACKOFF, minMs: 5_000 } : DEFAULT_BACKOFF;
    const delay = backoffDelay(this.#attempt, this.#options.random, policy);
    this.#attempt += 1;
    this.#setStatus({
      state: "waiting",
      attempt: this.#attempt,
      retryAt: Date.now() + delay,
      lastCode: code,
    });
    this.#retryTimer = this.#timer(() => {
      this.#retryTimer = null;
      this.#connect();
    }, delay);
  }

  #onConnectivity(online: boolean): void {
    if (online && this.#status.state === "offline") {
      this.#attempt = 0;
      this.#connect();
    } else if (!online && this.#status.state === "waiting") {
      if (this.#retryTimer) clearTimeout(this.#retryTimer);
      this.#retryTimer = null;
      this.#setStatus({ state: "offline" });
    }
  }

  #onVisibility(hidden: boolean): void {
    if (hidden) {
      if (this.#hiddenTimer) return;
      this.#hiddenTimer = this.#timer(() => {
        this.#hiddenTimer = null;
        if (this.#status.state === "stopped" || this.#status.state === "signedOut") return;
        if (this.#retryTimer) clearTimeout(this.#retryTimer);
        this.#retryTimer = null;
        this.#dropSocket(CloseCode.Normal, "tab hidden");
        this.#setStatus({ state: "paused" });
      }, this.#options.hiddenGraceMs);
      return;
    }
    if (this.#hiddenTimer) {
      clearTimeout(this.#hiddenTimer);
      this.#hiddenTimer = null;
    }
    if (this.#status.state === "paused") {
      this.#attempt = 0;
      this.#connect();
    }
  }

  /* ---------------------------------------------------------------- frames */

  #onMessage(data: unknown): void {
    if (data instanceof ArrayBuffer) {
      this.#emit({ type: "positions", data });
      return;
    }
    if (typeof data !== "string") return;
    let json: unknown;
    try {
      json = JSON.parse(data);
    } catch {
      this.#emit({ type: "protocolError", message: "text frame is not JSON" });
      return;
    }
    const kind = (json as { type?: unknown })?.type;
    const parsed = v.safeParse(ServerFrameSchema, json);
    if (!parsed.success) {
      // Unknown frame types are ignored on purpose: newer servers may add them.
      if (["hello", "event", "pulse", "sessions", "resync", "ops", "pong"].includes(String(kind))) {
        const issue = parsed.issues[0];
        const path = issue ? v.getDotPath(issue) : null;
        this.#emit({
          type: "protocolError",
          message: `malformed ${String(kind)} frame${path ? ` at ${path}` : ""}`,
        });
      }
      return;
    }
    const frame = parsed.output;
    switch (frame.type) {
      case "hello":
        this.#onHello(frame);
        break;
      case "event":
        this.#onEvent(frame.seq, frame.prev, frame.event);
        break;
      case "pulse":
        this.#emit({ type: "pulse", frame });
        break;
      case "sessions":
        this.#emit({ type: "sessions", sessions: frame.sessions });
        break;
      case "resync":
        // Positions are paused until the viewport arrives again.
        this.#emit({ type: "resync" });
        if (this.#viewport) this.#sendViewport(this.#viewport);
        break;
      case "ops":
        this.#emit({ type: "ops", frame });
        break;
      case "pong":
        this.#onPong(frame.t, frame.server_time);
        break;
    }
  }

  #onHello(hello: HelloFrame): void {
    this.#hello = hello;
    if (this.#helloDeadline) clearTimeout(this.#helloDeadline);
    this.#helloDeadline = null;
    this.clock.hint(hello.server_time, Date.now());
    this.#replayUntilMs = hello.resume.mode === "replay" ? hello.server_time : 0;
    // `resume.after` is the sequence after which this socket's events continue — for a replay,
    // a fresh start and a reset alike — so the chain (and what we persist) starts there.
    this.#sequencer.reset(hello.resume.after);
    this.#options.seqStore.set(hello.resume.after);
    if (hello.resume.mode === "reset") this.#emit({ type: "reset" });
    this.#setStatus({ state: "open", since: Date.now(), resume: hello.resume.mode });
    this.#emit({ type: "hello", hello });
    if (this.#viewport && this.#viewport !== this.#sentViewport) this.#sendViewport(this.#viewport);
    if (this.#ops) this.#send({ type: "ops", on: true });
    this.#startHeartbeat();
    if (this.#stableTimer) clearTimeout(this.#stableTimer);
    this.#stableTimer = this.#timer(() => {
      this.#attempt = 0;
      this.#stableTimer = null;
    }, this.#options.stableAfterMs);
  }

  #onEvent(seq: number, prev: number, payload: unknown): void {
    const verdict = this.#sequencer.judge(seq, prev);
    if (verdict === "duplicate") return;
    if (verdict === "gap") {
      // Something between `last` and `prev` never reached us: resume from `last` so the server
      // replays the whole range, this event included.
      this.#emit({ type: "protocolError", message: `event gap after ${this.#sequencer.last}` });
      this.#dropSocket(LOCAL_GAP, "event gap");
      this.#resumeNow(LOCAL_GAP);
      return;
    }
    this.#sequencer.commit(seq);
    this.#options.seqStore.set(seq);
    const parsed = v.safeParse(EventEnvelopeSchema, payload);
    if (!parsed.success) {
      this.#emit({ type: "protocolError", message: `unreadable event ${seq}` });
      return;
    }
    const occurredMs = Date.parse(parsed.output.ts);
    const replayed = this.#replayUntilMs > 0 && occurredMs <= this.#replayUntilMs;
    this.#emit({ type: "event", seq, event: parsed.output, replayed });
  }

  /* ---------------------------------------------------------------- liveness */

  #startHeartbeat(): void {
    this.#stopHeartbeat();
    this.#ping();
    this.#pingTimer = setInterval(() => this.#ping(), this.#options.pingIntervalMs);
  }

  #stopHeartbeat(): void {
    if (this.#pingTimer) clearInterval(this.#pingTimer);
    if (this.#pongDeadline) clearTimeout(this.#pongDeadline);
    if (this.#stableTimer) clearTimeout(this.#stableTimer);
    this.#pingTimer = null;
    this.#pongDeadline = null;
    this.#stableTimer = null;
  }

  #ping(): void {
    // Arm the deadline before sending, so an answer can never arrive ahead of it.
    const armed = this.#pongDeadline === null;
    if (armed) {
      this.#pongDeadline = this.#timer(() => {
        this.#pongDeadline = null;
        // No answer: the socket is half-open (sleeping laptop, NAT timeout). Replace it.
        const socket = this.#socket;
        this.#dropSocket(LOCAL_DEAD, "no pong");
        if (socket) this.#scheduleReconnect(LOCAL_DEAD);
      }, this.#options.pongTimeoutMs);
    }
    if (!this.#send({ type: "ping", t: Date.now() }) && armed && this.#pongDeadline) {
      clearTimeout(this.#pongDeadline);
      this.#pongDeadline = null;
    }
  }

  #onPong(sentAt: number, serverMs: number): void {
    if (this.#pongDeadline) clearTimeout(this.#pongDeadline);
    this.#pongDeadline = null;
    const now = Date.now();
    if (Number.isFinite(serverMs)) this.clock.sample(sentAt, serverMs, now);
    this.#emit({ type: "latency", rttMs: Math.max(0, now - sentAt) });
  }

  /* ---------------------------------------------------------------- plumbing */

  #send(message: object): boolean {
    const socket = this.#socket;
    if (socket?.readyState !== 1 || !this.#hello) return false;
    socket.send(JSON.stringify(message));
    return true;
  }

  #setStatus(status: LiveStatus): void {
    this.#status = status;
    this.#emit({ type: "status", status });
  }

  #emit(event: LiveEvent): void {
    for (const listener of this.#listeners) {
      try {
        listener(event);
      } catch (error) {
        console.error("live listener failed", error);
      }
    }
  }

  #timer(fn: () => void, ms: number): ReturnType<typeof setTimeout> {
    const handle = setTimeout(() => {
      this.#timers.delete(handle);
      fn();
    }, ms);
    this.#timers.add(handle);
    return handle;
  }

  #clearTimers(): void {
    for (const handle of this.#timers) clearTimeout(handle);
    this.#timers.clear();
    this.#retryTimer = null;
    this.#hiddenTimer = null;
    this.#stopHeartbeat();
  }

  get lastCloseCode(): number | null {
    return this.#lastCode;
  }
}

/** `last_seq` kept per tab and per user, so a reload resumes exactly where the tab left off. */
export function sessionSeqStore(userId: string, storage: Storage = sessionStorage): SeqStore {
  const key = `perimeter.lastSeq.${userId}`;
  return {
    get() {
      try {
        const raw = storage.getItem(key);
        const value = raw === null ? Number.NaN : Number(raw);
        return Number.isSafeInteger(value) && value >= 0 ? value : null;
      } catch {
        return null;
      }
    },
    set(seq) {
      try {
        if (seq === null) storage.removeItem(key);
        else storage.setItem(key, String(seq));
      } catch {
        // Storage unavailable (private mode quota): resume degrades to a fresh start.
      }
    },
  };
}
