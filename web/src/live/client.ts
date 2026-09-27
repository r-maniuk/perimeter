/**
 * The realtime channel (`/v1/live`, spec §12): one WebSocket per tab carrying binary position
 * bundles and JSON frames.
 *
 * Responsibilities kept here, independent of React and testable with a fake socket:
 * - connection lifecycle with exponential back-off and jitter, honouring the close codes
 *   (4001 signed out, 4002 session expired, 4003 forbidden, 4008 resume now, 4009 too many
 *   sessions, 1013 overloaded, 1008 a message of ours refused);
 * - exactly-once events: `seq`/`prev` sequencing, `last_seq` persisted per tab, `resume_after` on
 *   every reconnect, a client-side gap check that reconnects to heal;
 * - one account: a socket whose hello speaks for another user (the browser signed in as someone
 *   else in another tab) is refused before any of that account's data is taken in;
 * - liveness: application ping/pong with a deadline (half-open sockets are detected and replaced),
 *   checked at once when the tab wakes or the network returns, latency and server clock offset;
 * - re-sending the viewport and the ops subscription after every (re)connect and on `resync`, and
 *   otherwise sending a viewport only when it covers other tiles, a few times a second at most;
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
  type User,
} from "@/api/schemas";
import { foldBBox, tileSpan } from "@/lib/tiles";
import { backoffDelay, DEFAULT_BACKOFF } from "./backoff";
import { ClockSync } from "./clock";
import { EventSequencer } from "./sequencer";

export const CloseCode = {
  Normal: 1000,
  GoingAway: 1001,
  /** The server refused a message of this client's: it is not part of the protocol. */
  PolicyViolation: 1008,
  Internal: 1011,
  Overloaded: 1013,
  SignedOut: 4001,
  SessionExpired: 4002,
  Forbidden: 4003,
  EventOverflow: 4008,
  TooManySessions: 4009,
} as const;

/** Close codes the client uses itself (never sent by the server). */
const LOCAL_GAP = 4900;
const LOCAL_DEAD = 4901;
const LOCAL_NO_HELLO = 4902;
const LOCAL_OTHER_ACCOUNT = 4903;
const IMMEDIATE_RESUME_SPACING_MS = 5_000;
/**
 * Viewport messages go out at most this often. The server closes a socket that sends more than 20
 * messages a second, and a camera following a device moves on every frame.
 */
const VIEWPORT_SPACING_MS = 250;
/** The server's `LIVE_TILE_ZOOM` default, until `hello.tile_zoom` says otherwise. */
const DEFAULT_TILE_ZOOM = 12;
/** The zoom range a viewport message may carry (the protocol's `MapZoom`). */
const VIEWPORT_MIN_ZOOM = 0;
const VIEWPORT_MAX_ZOOM = 30;

type Timer = ReturnType<typeof setTimeout>;

/** Clear a pending timeout, if any; returns the `null` its holder resets to. */
function cancel(timer: Timer | null): null {
  if (timer) clearTimeout(timer);
  return null;
}

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
  | { state: "signedOut"; expired: boolean }
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
  /** A hello announced another account than the client's: that socket was refused. */
  | { type: "accountChanged"; user: User }
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
  /**
   * The account the client streams for. Sockets authenticate with the browser's session cookie,
   * which signing in elsewhere in the same browser replaces, so a hello may speak for someone
   * else; such a socket is refused (and retried with back-off) rather than trusted.
   */
  userId?: string | null;
  createSocket?: (url: string) => SocketLike;
  environment?: Environment;
  random?: () => number;
  pingIntervalMs?: number;
  pongTimeoutMs?: number;
  /**
   * How long an open socket may take to answer the check made when the tab is shown again or the
   * network comes back — the moments a socket most often died without a word (a laptop slept, a
   * network changed under it).
   */
  probeTimeoutMs?: number;
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
  /** The viewport last sent on the current socket, and when. */
  #sentViewport: Viewport | null = null;
  #sentViewportAt = Number.NEGATIVE_INFINITY;
  #viewportTimer: Timer | null = null;
  #tileZoom = DEFAULT_TILE_ZOOM;
  #ops = false;
  #hello: HelloFrame | null = null;
  #replayUntilMs = 0;
  // Every pending timer lives in one of these fields, so stopping clears them all.
  #retryTimer: Timer | null = null;
  #hiddenTimer: Timer | null = null;
  #pingTimer: ReturnType<typeof setInterval> | null = null;
  #pongDeadline: Timer | null = null;
  #pongDueAt = 0;
  #stableTimer: Timer | null = null;
  #helloDeadline: Timer | null = null;
  #lastImmediateAt = Number.NEGATIVE_INFINITY;
  #unsubscribe: (() => void)[] = [];

  constructor(options: LiveClientOptions) {
    this.#options = {
      createSocket: (url) => new WebSocket(url),
      environment: browserEnvironment,
      random: Math.random,
      pingIntervalMs: 15_000,
      pongTimeoutMs: 10_000,
      probeTimeoutMs: 5_000,
      hiddenGraceMs: 120_000,
      stableAfterMs: 10_000,
      helloTimeoutMs: 10_000,
      userId: null,
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
    // A tab opened in the background (or restored with the session) gets no visibility change
    // until it is shown: it keeps its socket exactly as long as a tab hidden later would.
    if (env.hidden()) this.#onVisibility(true);
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
    this.#offerViewport();
  }

  /**
   * Bring the server up to date with the latest viewport when that changes what it streams. The
   * server derives nothing from a viewport but the tiles covering it, so a view still over the
   * same leaf tiles needs no message; one that does waits out the spacing since the last one.
   */
  #offerViewport(): void {
    const viewport = this.#viewport;
    if (!viewport || this.#viewportTimer || this.#socket?.readyState !== 1) return;
    const sent = this.#sentViewport;
    if (sent && this.#span(sent) === this.#span(viewport)) return;
    const wait = this.#sentViewportAt + VIEWPORT_SPACING_MS - Date.now();
    if (wait > 0) {
      this.#viewportTimer = setTimeout(() => {
        this.#viewportTimer = null;
        this.#offerViewport();
      }, wait);
      return;
    }
    this.#sendViewport(viewport);
  }

  #span(viewport: Viewport): string {
    const [west, south, east, north] = viewport.bbox;
    return tileSpan({ west, south, east, north }, this.#tileZoom);
  }

  #sendViewport(viewport: Viewport): void {
    const socket = this.#socket;
    if (socket?.readyState !== 1) return;
    this.#viewportTimer = cancel(this.#viewportTimer);
    // The map reports unwrapped longitudes once panned around the world; the protocol wants them
    // within ±540°, so they go out folded into the first world (the covered tiles are the same).
    const [west, south, east, north] = viewport.bbox;
    const box = foldBBox({ west, south, east, north });
    const bbox = [box.west, box.south, box.east, box.north];
    // A zoom outside the protocol's range gets the socket closed, and every new socket would send
    // the same viewport again. The server streams by the tiles covering the box, not by the zoom,
    // so clamping it changes nothing that is streamed.
    const zoom = Math.min(VIEWPORT_MAX_ZOOM, Math.max(VIEWPORT_MIN_ZOOM, viewport.zoom));
    socket.send(JSON.stringify({ type: "viewport", bbox, zoom }));
    this.#sentViewport = viewport;
    this.#sentViewportAt = Date.now();
  }

  setOps(on: boolean): void {
    if (this.#ops === on) return;
    this.#ops = on;
    this.#send({ type: "ops", on });
  }

  /* ---------------------------------------------------------------- connection */

  #connect(): void {
    this.#retryTimer = cancel(this.#retryTimer);
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
    this.#sentViewportAt = Number.NEGATIVE_INFINITY;
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
    this.#helloDeadline = setTimeout(() => {
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
    this.#helloDeadline = cancel(this.#helloDeadline);
    this.#viewportTimer = cancel(this.#viewportTimer);
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
    this.#helloDeadline = cancel(this.#helloDeadline);
    this.#viewportTimer = cancel(this.#viewportTimer);
    this.#lastCode = code;
    switch (code) {
      case CloseCode.SignedOut:
      case CloseCode.SessionExpired:
        this.#setStatus({ state: "signedOut", expired: code === CloseCode.SessionExpired });
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
      case CloseCode.PolicyViolation:
        // Something this client sent was refused, and a new socket starts by sending the same
        // viewport: say what it was, and come back no sooner than to an overloaded server.
        this.#emit({
          type: "protocolError",
          message: `the server refused a message: ${reason || "policy violation"}`,
        });
        this.#scheduleReconnect(code);
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
      code === CloseCode.Overloaded || code === CloseCode.PolicyViolation
        ? { ...DEFAULT_BACKOFF, minMs: 5_000 }
        : DEFAULT_BACKOFF;
    const delay = backoffDelay(this.#attempt, this.#options.random, policy);
    this.#attempt += 1;
    this.#setStatus({
      state: "waiting",
      attempt: this.#attempt,
      retryAt: Date.now() + delay,
      lastCode: code,
    });
    this.#retryTimer = setTimeout(() => {
      this.#retryTimer = null;
      this.#connect();
    }, delay);
  }

  #onConnectivity(online: boolean): void {
    if (online && this.#status.state === "offline") {
      this.#attempt = 0;
      this.#connect();
    } else if (online) {
      // The route may have changed under an open socket (another network, a VPN): check it.
      this.#probe();
    } else if (this.#status.state === "waiting") {
      this.#retryTimer = cancel(this.#retryTimer);
      this.#setStatus({ state: "offline" });
    }
  }

  #onVisibility(hidden: boolean): void {
    if (hidden) {
      if (this.#hiddenTimer) return;
      this.#hiddenTimer = setTimeout(() => {
        this.#hiddenTimer = null;
        if (this.#status.state === "stopped" || this.#status.state === "signedOut") return;
        this.#retryTimer = cancel(this.#retryTimer);
        this.#dropSocket(CloseCode.Normal, "tab hidden");
        this.#setStatus({ state: "paused" });
      }, this.#options.hiddenGraceMs);
      return;
    }
    this.#hiddenTimer = cancel(this.#hiddenTimer);
    if (this.#status.state === "paused") {
      this.#attempt = 0;
      this.#connect();
      return;
    }
    // Background tabs and sleeping machines barely run timers: the heartbeat may not have noticed
    // a socket that died meanwhile, and the connection would pass for live until it did.
    this.#probe();
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
    const expected = this.#options.userId;
    if (expected !== null && hello.user.id !== expected) {
      // Nothing of the other account's stream is taken in, not even its sequence numbers, which
      // would otherwise become this user's resume point. Its listeners find out who is signed in.
      this.#dropSocket(LOCAL_OTHER_ACCOUNT, "another account");
      this.#scheduleReconnect(LOCAL_OTHER_ACCOUNT);
      this.#emit({ type: "accountChanged", user: hello.user });
      return;
    }
    this.#hello = hello;
    this.#tileZoom = hello.tile_zoom;
    this.#helloDeadline = cancel(this.#helloDeadline);
    this.clock.hint(hello.server_time, Date.now());
    this.#replayUntilMs = hello.resume.mode === "replay" ? hello.server_time : 0;
    // `resume.after` is the sequence after which this socket's events continue — for a replay,
    // a fresh start and a reset alike — so the chain (and what we persist) starts there.
    this.#sequencer.reset(hello.resume.after);
    this.#options.seqStore.set(hello.resume.after);
    if (hello.resume.mode === "reset") this.#emit({ type: "reset" });
    this.#setStatus({ state: "open", since: Date.now(), resume: hello.resume.mode });
    this.#emit({ type: "hello", hello });
    // The view may have moved since the socket opened (or opened without one).
    this.#offerViewport();
    if (this.#ops) this.#send({ type: "ops", on: true });
    this.#startHeartbeat();
    this.#stableTimer = cancel(this.#stableTimer);
    this.#stableTimer = setTimeout(() => {
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
    this.#pingTimer = null;
    this.#pongDeadline = cancel(this.#pongDeadline);
    this.#stableTimer = cancel(this.#stableTimer);
  }

  /**
   * Send a ping that must be answered within `timeoutMs`. Any pong clears the deadline, so one
   * already running covers this ping too when it is the sooner of the two.
   */
  #ping(timeoutMs: number = this.#options.pongTimeoutMs): void {
    // Arm the deadline before sending, so an answer can never arrive ahead of it.
    const dueAt = Date.now() + timeoutMs;
    const armed = this.#pongDeadline === null || dueAt < this.#pongDueAt;
    if (armed) {
      this.#pongDeadline = cancel(this.#pongDeadline);
      this.#pongDueAt = dueAt;
      this.#pongDeadline = setTimeout(() => {
        this.#pongDeadline = null;
        // No answer: the socket is half-open (sleeping laptop, NAT timeout). Replace it.
        const socket = this.#socket;
        this.#dropSocket(LOCAL_DEAD, "no pong");
        if (socket) this.#scheduleReconnect(LOCAL_DEAD);
      }, timeoutMs);
    }
    if (!this.#send({ type: "ping", t: Date.now() }) && armed) {
      this.#pongDeadline = cancel(this.#pongDeadline);
    }
  }

  /** Make an open socket prove it is alive, soon. */
  #probe(): void {
    if (this.#status.state === "open") this.#ping(this.#options.probeTimeoutMs);
  }

  #onPong(sentAt: number, serverMs: number): void {
    this.#pongDeadline = cancel(this.#pongDeadline);
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

  #clearTimers(): void {
    this.#retryTimer = cancel(this.#retryTimer);
    this.#hiddenTimer = cancel(this.#hiddenTimer);
    this.#helloDeadline = cancel(this.#helloDeadline);
    this.#viewportTimer = cancel(this.#viewportTimer);
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
