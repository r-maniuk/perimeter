import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoffDelay } from "./backoff";
import {
  CloseCode,
  type Environment,
  LiveClient,
  type LiveEvent,
  type SeqStore,
  type SocketLike,
  sessionSeqStore,
} from "./client";
import { ClockSync } from "./clock";
import { EventSequencer } from "./sequencer";

class FakeSocket implements SocketLike {
  /** Answer pings like a healthy server would. */
  autoPong = true;
  binaryType: BinaryType = "blob";
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closedWith: { code: number | undefined; reason: string | undefined } | null = null;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string): void {
    const message = JSON.parse(data) as Record<string, unknown>;
    this.sent.push(message);
    if (this.autoPong && message.type === "ping") {
      this.receive({ type: "pong", t: message.t, server_time: Date.now() });
    }
  }

  close(code?: number, reason?: string): void {
    this.closedWith = { code, reason };
    this.readyState = 3;
  }

  /* server side */
  accept(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  receive(frame: object | ArrayBuffer): void {
    const data = frame instanceof ArrayBuffer ? frame : JSON.stringify(frame);
    this.onmessage?.({ data } as MessageEvent);
  }

  drop(code: number, reason = ""): void {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  sentOfType(type: string) {
    return this.sent.filter((m) => m.type === type);
  }
}

class FakeEnvironment implements Environment {
  isOnline = true;
  isHidden = false;
  #connectivity = new Set<(online: boolean) => void>();
  #visibility = new Set<(hidden: boolean) => void>();
  online = () => this.isOnline;
  hidden = () => this.isHidden;
  onConnectivity(listener: (online: boolean) => void) {
    this.#connectivity.add(listener);
    return () => this.#connectivity.delete(listener);
  }
  onVisibility(listener: (hidden: boolean) => void) {
    this.#visibility.add(listener);
    return () => this.#visibility.delete(listener);
  }
  setOnline(online: boolean) {
    this.isOnline = online;
    for (const l of this.#connectivity) l(online);
  }
  setHidden(hidden: boolean) {
    this.isHidden = hidden;
    for (const l of this.#visibility) l(hidden);
  }
}

class MemoryStore implements SeqStore {
  constructor(public value: number | null = null) {}
  get = () => this.value;
  set = (seq: number | null) => {
    this.value = seq;
  };
}

const NOW = Date.parse("2026-09-26T19:00:00Z");

/** The first frame of every connection, as the server sends it. */
function hello(mode: "fresh" | "replay" | "reset" = "fresh", after = 0) {
  return {
    type: "hello",
    session_id: "0192f7e1-0000-7000-8000-000000000001",
    user: { id: "0192f7e0-0000-7000-8000-00000000000a", username: "ada" },
    server_time: NOW,
    protocol: 1,
    resume: { mode, after },
    tile_zoom: 12,
    replica: "api-1",
  };
}

function alertEvent(seq: number, prev: number, ts = new Date(NOW + 1000).toISOString()) {
  return {
    type: "event",
    seq,
    prev,
    event: {
      id: `evt-${seq}`,
      type: "alert",
      ts,
      data: {
        alert_id: `a-${seq}`,
        kind: "enter",
        device_id: "veh-1",
        zone: { id: "z-1", name: "Dam" },
        occurred_at: ts,
        position: { lat: 52.37, lon: 4.89 },
      },
    },
  };
}

function setup(
  store = new MemoryStore(),
  random = () => 0.5,
  autoPong = true,
  userId: string | null = null,
) {
  const sockets: FakeSocket[] = [];
  const env = new FakeEnvironment();
  const events: LiveEvent[] = [];
  const client = new LiveClient({
    url: "ws://test/v1/live",
    seqStore: store,
    environment: env,
    random,
    userId,
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      socket.autoPong = autoPong;
      sockets.push(socket);
      return socket;
    },
  });
  client.subscribe((e) => events.push(e));
  const last = () => sockets.at(-1) as FakeSocket;
  const open = (frame: object = hello()) => {
    last().accept();
    last().receive(frame);
  };
  const ofType = <T extends LiveEvent["type"]>(type: T) =>
    events.filter((e): e is Extract<LiveEvent, { type: T }> => e.type === type);
  return { client, sockets, env, events, store, last, open, ofType };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("connecting", () => {
  it("starts fresh without a stored sequence and resumes with one", () => {
    const fresh = setup();
    fresh.client.start();
    expect(fresh.last().url).toBe("ws://test/v1/live");
    expect(fresh.last().binaryType).toBe("arraybuffer");

    const resumed = setup(new MemoryStore(41));
    resumed.client.start();
    expect(resumed.last().url).toBe("ws://test/v1/live?resume_after=41");
  });

  it("opens on hello, then sends the viewport, the ops subscription and a ping", () => {
    const t = setup();
    t.client.setViewport({ bbox: [4.8, 52.3, 5.0, 52.4], zoom: 12.5 });
    t.client.setOps(true);
    t.client.start();
    expect(t.client.status.state).toBe("connecting");
    t.open();
    expect(t.client.status).toMatchObject({ state: "open", resume: "fresh" });
    expect(t.last().sentOfType("viewport")).toEqual([
      { type: "viewport", bbox: [4.8, 52.3, 5.0, 52.4], zoom: 12.5 },
    ]);
    expect(t.last().sentOfType("ops")).toEqual([{ type: "ops", on: true }]);
    expect(t.last().sentOfType("ping")).toHaveLength(1);
  });

  it("sends the viewport as the very first message, before hello", () => {
    const t = setup();
    t.client.setViewport({ bbox: [4.8, 52.3, 5.0, 52.4], zoom: 12 });
    t.client.start();
    t.last().accept();
    expect(t.last().sent).toEqual([{ type: "viewport", bbox: [4.8, 52.3, 5.0, 52.4], zoom: 12 }]);
    t.last().receive(hello());
    // Not repeated after hello when unchanged.
    expect(t.last().sentOfType("viewport")).toHaveLength(1);
  });

  it("re-sends an unchanged viewport on resync", () => {
    const t = setup();
    t.client.setViewport({ bbox: [1, 2, 3, 4], zoom: 9 });
    t.client.start();
    t.open();
    t.last().receive({ type: "resync", scope: "positions" });
    t.last().receive({ type: "resync", scope: "positions" });
    expect(t.last().sentOfType("viewport")).toHaveLength(3);
  });

  it("folds a viewport panned around the world into the first world", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.client.setViewport({ bbox: [724.79, 52.33, 724.99, 52.41], zoom: 12.3 });
    t.client.setViewport({ bbox: [-600, -80, 300, 80], zoom: 0.4 });
    vi.advanceTimersByTime(250);
    const sent = t.last().sentOfType("viewport") as { bbox: number[] }[];
    expect(sent[0]?.bbox[0]).toBeCloseTo(4.79, 9);
    expect(sent[0]?.bbox[2]).toBeCloseTo(4.99, 9);
    expect(sent[1]?.bbox).toEqual([-180, -80, 180, 80]);
  });

  it("sends viewport changes immediately once open", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.client.setViewport({ bbox: [1, 2, 3, 4], zoom: 9 });
    t.client.setOps(true);
    t.client.setOps(true);
    expect(t.last().sentOfType("viewport")).toHaveLength(1);
    expect(t.last().sentOfType("ops")).toHaveLength(1);
  });

  it("sends a viewport only when it covers other tiles than the last one sent", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.client.setViewport({ bbox: [4.79, 52.33, 4.99, 52.41], zoom: 12 });
    vi.advanceTimersByTime(1_000);
    // A few metres of pan and a slight zoom stay on the same leaf tiles: nothing to tell.
    t.client.setViewport({ bbox: [4.7901, 52.3301, 4.9901, 52.4101], zoom: 12.01 });
    vi.advanceTimersByTime(1_000);
    expect(t.last().sentOfType("viewport")).toHaveLength(1);
    t.client.setViewport({ bbox: [4.89, 52.33, 5.09, 52.41], zoom: 12 });
    expect(t.last().sentOfType("viewport")).toEqual([
      expect.objectContaining({ bbox: [4.79, 52.33, 4.99, 52.41] }),
      expect.objectContaining({ bbox: [4.89, 52.33, 5.09, 52.41] }),
    ]);
  });

  it("keeps a camera that moves every frame under four viewport messages a second", () => {
    const t = setup();
    t.client.start();
    t.open();
    const socket = t.last();
    const sentAt: number[] = [];
    const send = socket.send.bind(socket);
    socket.send = (data: string) => {
      if (data.includes('"viewport"')) sentAt.push(Date.now());
      send(data);
    };
    // A camera gliding across tiles for ten seconds, reporting a viewport on every frame.
    const at = (frame: number): [number, number, number, number] => {
      const west = 4.85 + frame * 0.005;
      return [west, 52.36, west + 0.02, 52.37];
    };
    let frame = 0;
    for (; frame < 600; frame++) {
      t.client.setViewport({ bbox: at(frame), zoom: 15 });
      vi.advanceTimersByTime(1000 / 60);
    }
    expect(sentAt.length).toBeGreaterThan(30);
    for (const [i, time] of sentAt.entries()) {
      const inLastSecond = sentAt.filter((other) => other > time - 1_000 && other <= time);
      expect(inLastSecond.length).toBeLessThanOrEqual(4);
      if (i > 0) expect(time - (sentAt[i - 1] ?? 0)).toBeGreaterThanOrEqual(250);
    }
    // Once the camera rests, the server holds exactly where it stopped.
    vi.advanceTimersByTime(250);
    const last = socket.sentOfType("viewport").at(-1) as { bbox: number[] };
    expect(last.bbox).toEqual(at(frame - 1));
  });

  it("measures tiles at the zoom the server announces", () => {
    const t = setup();
    t.client.start();
    t.open({ ...hello(), tile_zoom: 16 });
    t.client.setViewport({ bbox: [4.9, 52.37, 4.91, 52.375], zoom: 16 });
    vi.advanceTimersByTime(1_000);
    // ~150 m east: the same tile at zoom 12, another one at zoom 16.
    t.client.setViewport({ bbox: [4.9022, 52.37, 4.9122, 52.375], zoom: 16 });
    expect(t.last().sentOfType("viewport")).toHaveLength(2);
  });

  it("forwards binary bundles untouched", () => {
    const t = setup();
    t.client.start();
    t.open();
    const buffer = new ArrayBuffer(8);
    t.last().receive(buffer);
    expect(t.ofType("positions")[0]?.data).toBe(buffer);
  });

  it("refuses a socket that speaks for another account, keeping this user's resume point", () => {
    const ada = hello().user.id;
    const t = setup(new MemoryStore(30), () => 1, true, "u-grace");
    t.client.start();
    t.open(hello("replay", 30));
    expect(t.ofType("accountChanged")).toEqual([
      { type: "accountChanged", user: { id: ada, username: "ada" } },
    ]);
    expect(t.ofType("hello")).toHaveLength(0);
    expect(t.events.some((e) => e.type === "status" && e.status.state === "open")).toBe(false);
    expect(t.store.value).toBe(30);
    expect(t.sockets[0]?.closedWith?.code).toBe(4903);
    // Anything the refused socket still delivers is ignored.
    t.sockets[0]?.receive(alertEvent(31, 30));
    expect(t.ofType("event")).toHaveLength(0);
    // It tries again after the usual back-off, still resuming where this user left off.
    expect(t.client.status).toMatchObject({ state: "waiting", lastCode: 4903 });
    vi.advanceTimersByTime(500);
    expect(t.sockets).toHaveLength(2);
    expect(t.last().url).toBe("ws://test/v1/live?resume_after=30");
    t.open({ ...hello("replay", 30), user: { id: "u-grace", username: "grace" } });
    expect(t.client.status.state).toBe("open");
  });

  it("replaces a socket that never says hello", () => {
    const t = setup();
    t.client.start();
    t.last().accept();
    vi.advanceTimersByTime(10_000);
    expect(t.client.status.state).toBe("waiting");
    vi.advanceTimersByTime(500);
    expect(t.sockets).toHaveLength(2);
  });
});

describe("events", () => {
  it("delivers in order, persists the sequence and drops duplicates", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.last().receive(alertEvent(5, 0));
    t.last().receive(alertEvent(7, 5));
    t.last().receive(alertEvent(7, 5));
    t.last().receive(alertEvent(6, 5));
    expect(t.ofType("event").map((e) => e.seq)).toEqual([5, 7]);
    expect(t.store.value).toBe(7);
    expect(t.client.lastSeq).toBe(7);
  });

  it("heals a gap by resuming from the last event it saw", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.last().receive(alertEvent(5, 0));
    t.last().receive(alertEvent(9, 8));
    expect(t.ofType("event").map((e) => e.seq)).toEqual([5]);
    expect(t.sockets).toHaveLength(2);
    expect(t.last().url).toBe("ws://test/v1/live?resume_after=5");
    t.open(hello("replay", 5));
    t.last().receive(alertEvent(8, 5));
    t.last().receive(alertEvent(9, 8));
    expect(t.ofType("event").map((e) => e.seq)).toEqual([5, 8, 9]);
  });

  it("flags events that happened while the tab was away", () => {
    const t = setup(new MemoryStore(3));
    t.client.start();
    t.open(hello("replay", 3));
    t.last().receive(alertEvent(4, 3, new Date(NOW - 60_000).toISOString()));
    t.last().receive(alertEvent(5, 4, new Date(NOW + 5_000).toISOString()));
    expect(t.ofType("event").map((e) => e.replayed)).toEqual([true, false]);
  });

  it("starts over when the server cannot replay from our position", () => {
    const t = setup(new MemoryStore(3));
    t.client.start();
    expect(t.last().url).toBe("ws://test/v1/live?resume_after=3");
    // Event 3 is no longer retained: the server resets and says where events continue.
    t.open(hello("reset", 850));
    expect(t.ofType("reset")).toHaveLength(1);
    expect(t.store.value).toBe(850);
    t.last().receive(alertEvent(900, 850));
    expect(t.ofType("event").map((e) => e.seq)).toEqual([900]);
    expect(t.store.value).toBe(900);
  });

  it("continues a fresh session's chain from hello.resume.after", () => {
    const t = setup();
    t.client.start();
    t.open(hello("fresh", 40));
    expect(t.store.value).toBe(40);
    expect(t.client.lastSeq).toBe(40);
    t.last().receive(alertEvent(40, 38));
    t.last().receive(alertEvent(41, 40));
    expect(t.ofType("event").map((e) => e.seq)).toEqual([41]);
    expect(t.ofType("reset")).toHaveLength(0);
  });

  it("reports unreadable frames without breaking the stream", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.last().receive({ type: "event", seq: 1, prev: 0, event: { nonsense: true } });
    t.last().receive({ type: "pulse", zones: "nope" });
    t.last().receive({ type: "from-the-future", x: 1 });
    t.last().onmessage?.({ data: "{not json" } as MessageEvent);
    t.last().receive(alertEvent(2, 1));
    expect(t.ofType("protocolError")).toHaveLength(3);
    expect(t.ofType("event").map((e) => e.seq)).toEqual([2]);
  });
});

describe("frames", () => {
  it("dispatches pulses, sessions, ops and resync (re-sending the viewport)", () => {
    const t = setup();
    t.client.setViewport({ bbox: [0, 0, 1, 1], zoom: 10 });
    t.client.start();
    t.open();
    t.last().receive({ type: "pulse", window_ms: 100, zones: { "z-1": ["a", "b"] } });
    t.last().receive({
      type: "sessions",
      sessions: [
        {
          sid: "s-1",
          label: "Chrome · macOS",
          agent: "Mozilla/5.0",
          ip: "10.0.0.7",
          replica: "api-1",
          connected_at: "2026-09-26T18:59:00Z",
          current: true,
        },
      ],
    });
    t.last().receive({
      type: "ops",
      services: [{ service: "api", instance: "api-1", ts: 1, loop_lag_p99_ms: 1.5 }],
      ts: 1,
    });
    t.last().receive({ type: "resync", scope: "positions" });
    expect(t.ofType("pulse")[0]?.frame.zones).toEqual({ "z-1": ["a", "b"] });
    expect(t.last().sent[0]).toMatchObject({ type: "viewport" });
    expect(t.ofType("sessions")[0]?.sessions[0]?.sid).toBe("s-1");
    expect(t.ofType("ops")[0]?.frame.services).toHaveLength(1);
    expect(t.ofType("resync")).toHaveLength(1);
    expect(t.last().sentOfType("viewport")).toHaveLength(2);
  });

  it("measures latency and the server clock from pongs", () => {
    const t = setup(new MemoryStore(), () => 0.5, false);
    t.client.start();
    t.open();
    const ping = t.last().sentOfType("ping")[0] as { t: number };
    vi.advanceTimersByTime(40);
    t.last().receive({ type: "pong", t: ping.t, server_time: NOW + 60_000 + 20 });
    expect(t.ofType("latency")[0]?.rttMs).toBe(40);
    expect(t.client.clock.offsetMs).toBeCloseTo(60_000, 0);
  });
});

describe("reconnecting", () => {
  it("backs off with jitter and grows the ceiling per attempt", () => {
    const t = setup(new MemoryStore(), () => 1);
    t.client.start();
    t.last().drop(1006);
    expect(t.client.status).toMatchObject({ state: "waiting", attempt: 1, lastCode: 1006 });
    vi.advanceTimersByTime(499);
    expect(t.sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(2);
    t.last().drop(1011);
    vi.advanceTimersByTime(999);
    expect(t.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(3);
  });

  it("waits at least five seconds when the server is overloaded", () => {
    const t = setup(new MemoryStore(), () => 0);
    t.client.start();
    t.last().drop(CloseCode.Overloaded);
    vi.advanceTimersByTime(4_999);
    expect(t.sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(2);
  });

  it("resumes immediately after an event-queue overflow, with the last sequence", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.last().receive(alertEvent(12, 0));
    t.last().drop(CloseCode.EventOverflow);
    expect(t.sockets).toHaveLength(2);
    expect(t.last().url).toBe("ws://test/v1/live?resume_after=12");
  });

  it("does not loop on repeated immediate resumes", () => {
    const t = setup(new MemoryStore(), () => 1);
    t.client.start();
    t.open();
    t.last().drop(CloseCode.EventOverflow);
    t.open();
    t.last().drop(CloseCode.EventOverflow);
    expect(t.client.status.state).toBe("waiting");
  });

  it.each([1001, 1006, 1011, 1012, 1013, 4008])(
    "reconnects after close code %i, resuming after the last event",
    (code) => {
      const t = setup();
      t.client.start();
      t.open(hello("fresh", 30));
      t.last().receive(alertEvent(31, 30));
      t.last().drop(code);
      for (let waited = 0; t.sockets.length < 2 && waited < 60_000; waited += 100) {
        vi.advanceTimersByTime(100);
      }
      expect(t.sockets).toHaveLength(2);
      expect(t.last().url).toBe("ws://test/v1/live?resume_after=31");
    },
  );

  it.each([
    [4001, "signedOut"],
    [4003, "blocked"],
    [4009, "blocked"],
  ] as const)("stays closed after close code %i (%s)", (code, state) => {
    const t = setup();
    t.client.start();
    t.open();
    t.last().drop(code);
    vi.advanceTimersByTime(10 * 60_000);
    expect(t.sockets).toHaveLength(1);
    expect(t.client.status.state).toBe(state);
  });

  it("stops on sign-out and on a session cap, until asked to retry", () => {
    const signedOut = setup();
    signedOut.client.start();
    signedOut.open();
    signedOut.last().drop(CloseCode.SignedOut);
    vi.advanceTimersByTime(60_000);
    expect(signedOut.client.status.state).toBe("signedOut");
    expect(signedOut.sockets).toHaveLength(1);

    const capped = setup();
    capped.client.start();
    capped.last().drop(CloseCode.TooManySessions, "limit 16");
    vi.advanceTimersByTime(60_000);
    expect(capped.client.status).toEqual({ state: "blocked", code: 4009, reason: "limit 16" });
    capped.client.retryNow();
    expect(capped.sockets).toHaveLength(2);
  });

  it("resets the back-off after a stable connection", () => {
    const t = setup(new MemoryStore(), () => 1);
    t.client.start();
    t.last().drop(1006);
    vi.advanceTimersByTime(500);
    t.last().drop(1006);
    vi.advanceTimersByTime(1_000);
    t.open();
    expect(t.client.status).toMatchObject({ state: "open" });
    vi.advanceTimersByTime(10_000);
    t.last().drop(1001);
    expect(t.client.status).toMatchObject({ state: "waiting", attempt: 1 });
  });

  it("replaces a half-open socket that stops answering pings", () => {
    const t = setup(new MemoryStore(), () => 0, false);
    t.client.start();
    t.open();
    vi.advanceTimersByTime(10_000);
    expect(t.sockets[0]?.closedWith?.code).toBe(4901);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(2);
  });

  it("checks the socket the moment the tab is shown again, and replaces a dead one quickly", () => {
    const t = setup(new MemoryStore(), () => 0, false);
    t.client.start();
    t.open();
    const socket = t.last();
    // The first ping of the connection was answered; then the laptop sleeps with the tab hidden.
    socket.receive({ type: "pong", t: Date.now(), server_time: Date.now() });
    t.env.setHidden(true);
    vi.advanceTimersByTime(4_000);
    const pings = socket.sentOfType("ping").length;
    t.env.setHidden(false);
    expect(socket.sentOfType("ping")).toHaveLength(pings + 1);
    vi.advanceTimersByTime(4_999);
    expect(t.client.status.state).toBe("open");
    vi.advanceTimersByTime(1);
    expect(socket.closedWith?.code).toBe(4901);
    expect(t.client.status.state).toBe("waiting");
  });

  it("checks the socket when the network comes back", () => {
    const t = setup(new MemoryStore(), () => 0, false);
    t.client.start();
    t.open();
    const socket = t.last();
    socket.receive({ type: "pong", t: Date.now(), server_time: Date.now() });
    vi.advanceTimersByTime(3_000);
    t.env.setOnline(true);
    expect(socket.sentOfType("ping")).toHaveLength(2);
    vi.advanceTimersByTime(5_000);
    expect(socket.closedWith?.code).toBe(4901);
  });

  it("keeps a socket that answers the check", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.env.setHidden(true);
    t.env.setHidden(false);
    t.env.setOnline(true);
    vi.advanceTimersByTime(60_000);
    expect(t.client.status.state).toBe("open");
    expect(t.sockets).toHaveLength(1);
  });

  it("never lets a check postpone a ping that is already overdue sooner", () => {
    const t = setup(new MemoryStore(), () => 0, false);
    t.client.start();
    t.open();
    // The first ping is due within 10 s; 8 s in, a check must not push that out to 13 s.
    vi.advanceTimersByTime(8_000);
    t.env.setHidden(false);
    vi.advanceTimersByTime(2_000);
    expect(t.last().closedWith?.code).toBe(4901);
  });

  it("waits for connectivity instead of burning attempts offline", () => {
    const t = setup();
    t.client.start();
    t.env.isOnline = false;
    t.last().drop(1006);
    expect(t.client.status.state).toBe("offline");
    vi.advanceTimersByTime(60_000);
    expect(t.sockets).toHaveLength(1);
    t.env.setOnline(true);
    expect(t.sockets).toHaveLength(2);
  });

  it("releases the socket in a long-hidden tab and resumes when visible again", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.last().receive(alertEvent(30, 0));
    t.env.setHidden(true);
    vi.advanceTimersByTime(119_999);
    expect(t.client.status.state).toBe("open");
    vi.advanceTimersByTime(1);
    expect(t.client.status.state).toBe("paused");
    expect(t.sockets[0]?.closedWith?.code).toBe(1000);
    t.env.setHidden(false);
    expect(t.last().url).toBe("ws://test/v1/live?resume_after=30");
  });

  it("releases the socket of a tab that opened in the background, like one hidden later", () => {
    const t = setup(new MemoryStore(12));
    t.env.isHidden = true;
    t.client.start();
    t.open(hello("replay", 12));
    vi.advanceTimersByTime(119_999);
    expect(t.client.status.state).toBe("open");
    vi.advanceTimersByTime(1);
    expect(t.client.status.state).toBe("paused");
    expect(t.sockets[0]?.closedWith?.code).toBe(1000);
    vi.advanceTimersByTime(10 * 60_000);
    expect(t.sockets).toHaveLength(1);
    t.env.setHidden(false);
    expect(t.sockets).toHaveLength(2);
    expect(t.last().url).toBe("ws://test/v1/live?resume_after=12");
  });

  it("keeps the socket of a background tab that is shown within the grace period", () => {
    const t = setup();
    t.env.isHidden = true;
    t.client.start();
    t.open();
    vi.advanceTimersByTime(60_000);
    t.env.setHidden(false);
    vi.advanceTimersByTime(10 * 60_000);
    expect(t.client.status.state).toBe("open");
    expect(t.sockets).toHaveLength(1);
  });

  it("stops cleanly", () => {
    const t = setup();
    t.client.start();
    t.open();
    t.client.stop();
    expect(t.client.status.state).toBe("stopped");
    expect(t.sockets[0]?.closedWith?.code).toBe(1000);
    vi.advanceTimersByTime(120_000);
    expect(t.sockets).toHaveLength(1);
  });

  it("leaves no timer behind when stopped, whatever was pending", () => {
    const t = setup(new MemoryStore(), () => 0.5, false);
    t.client.start();
    t.open();
    // A pending pong deadline, the heartbeat, the stable-connection timer, a hidden-tab grace
    // timer and a viewport waiting for its turn.
    t.client.setViewport({ bbox: [4.79, 52.33, 4.99, 52.41], zoom: 12 });
    t.client.setViewport({ bbox: [5.79, 52.33, 5.99, 52.41], zoom: 12 });
    t.env.setHidden(true);
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(5);
    t.client.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("holds a steady set of timers through a long session", () => {
    const t = setup();
    t.client.start();
    t.open();
    vi.advanceTimersByTime(10_000);
    const settled = vi.getTimerCount();
    for (let beat = 0; beat < 200; beat++) {
      vi.advanceTimersByTime(15_000);
      t.client.setViewport({ bbox: [4 + beat * 0.2, 52.33, 4.2 + beat * 0.2, 52.41], zoom: 12 });
    }
    vi.advanceTimersByTime(1_000);
    expect(t.last().sentOfType("ping").length).toBeGreaterThan(200);
    expect(vi.getTimerCount()).toBe(settled);
  });
});

describe("helpers", () => {
  it("full-jitter back-off stays within its ceiling", () => {
    expect(backoffDelay(0, () => 1)).toBe(500);
    expect(backoffDelay(3, () => 0.5)).toBe(2_000);
    expect(backoffDelay(20, () => 1)).toBe(20_000);
    expect(backoffDelay(2, () => 0, { baseMs: 500, capMs: 20_000, minMs: 5_000 })).toBe(5_000);
  });

  it("sequencer judges duplicates and gaps", () => {
    const s = new EventSequencer();
    expect(s.judge(10, 3)).toBe("deliver");
    s.commit(10);
    expect(s.judge(10, 3)).toBe("duplicate");
    expect(s.judge(12, 10)).toBe("deliver");
    expect(s.judge(14, 12)).toBe("gap");
    s.commit(9);
    expect(s.last).toBe(10);
  });

  it("clock sync trusts the fastest round trip", () => {
    const clock = new ClockSync();
    clock.hint(10_000, 9_000);
    expect(clock.offsetMs).toBe(1_000);
    clock.sample(0, 5_100, 200);
    clock.sample(1_000, 6_010, 1_020);
    expect(clock.offsetMs).toBe(5_000);
    expect(clock.now(100)).toBe(5_100);
  });

  it("stores the sequence per user and tolerates bad values", () => {
    const backing = new Map<string, string>();
    const storage = {
      getItem: (k: string) => backing.get(k) ?? null,
      setItem: (k: string, v: string) => backing.set(k, v),
      removeItem: (k: string) => backing.delete(k),
    } as unknown as Storage;
    const store = sessionSeqStore("u-1", storage);
    expect(store.get()).toBeNull();
    store.set(42);
    expect(store.get()).toBe(42);
    expect(sessionSeqStore("u-2", storage).get()).toBeNull();
    backing.set("perimeter.lastSeq.u-1", "-3");
    expect(store.get()).toBeNull();
    store.set(null);
    expect(backing.size).toBe(0);
  });
});
