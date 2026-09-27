/**
 * The signed-in runtime: one live connection, the fleet it feeds, and the glue that turns live
 * frames into cache updates, store changes and map effects. Created on sign-in, torn down on
 * sign-out; nothing here depends on React.
 */
import type { QueryClient } from "@tanstack/react-query";
import { alertFromRecord, currentUser, getZone, updateZone } from "@/api/endpoints";
import type { EventEnvelope, HelloFrame, OpsFrame, User, Zone } from "@/api/schemas";
import { seriesOf } from "@/features/ops/model";
import { notify } from "@/features/shell/notices";
import {
  applyZoneEvent,
  bumpOccupancy,
  readZones,
  ZONES_KEY,
  ZonePatcher,
} from "@/features/zones/model";
import { FleetStore } from "@/fleet/store";
import { hexToRgba, packRgba } from "@/lib/color";
import { decodeBundleFrames, FrameError } from "@/lib/frames";
import { RecentSet } from "@/lib/recent";
import { coveredBy, coveringQuadkeys, quadkey } from "@/lib/tiles";
import { LiveClient, type LiveEvent, liveUrl, sessionSeqStore, type Viewport } from "@/live/client";
import { mapController } from "@/map/controller";
import { useAlerts } from "@/state/alerts";
import { useLive } from "@/state/live";
import { useSession } from "@/state/session";
import { useUi } from "@/state/ui";

export const SESSIONS_KEY = ["sessions"] as const;

/**
 * Devices silent this long leave the map: the server's `LIVE_DEVICE_STALE_S` default (snapshots
 * already leave such devices out; this sweep covers tiles that stopped sending anything).
 */
const STALE_S = 600;
const SWEEP_MS = 5_000;
const COUNTERS_MS = 1_000;
const REPORTING_WINDOW_MS = 6_000;
/** The server's `LIVE_MAX_VIEWPORT_TILES` default: the same cover of the viewport it streams. */
const MAX_VIEWPORT_TILES = 16;
/** Alerts remembered to recognise one delivered twice (a few minutes of a busy fleet). */
const ALERT_MEMORY = 4_096;
/** A zone's occupant list is read again at most this often while alerts keep arriving. */
const OCCUPANTS_REFRESH_MS = 1_000;

export class Runtime {
  readonly user: User;
  readonly fleet = new FleetStore({ capacity: 16_384 });
  readonly live: LiveClient;
  readonly patcher: ZonePatcher;
  #client: QueryClient;
  #timers: ReturnType<typeof setInterval>[] = [];
  #unsubscribe: (() => void)[] = [];
  #tileZoom = 12;
  #viewport: Viewport | null = null;
  /** The tile prefixes covering the viewport: what the server is asked to stream. */
  #prefixes: string[] | null = null;
  #cover: string | null = null;
  #animate = new Set<string>();
  /** What the map was last given to draw (nothing yet), and what the fleet tints devices with. */
  #drawnZones: string | null = null;
  #tintZones = "";
  /** A zone event is being folded in: it syncs the map itself, once, with its animation. */
  #holdZoneSync = false;
  #occupantRefresh = new Map<string, { again: boolean; timer: ReturnType<typeof setTimeout> }>();
  #reporting = new Map<string, Map<string, number>>();
  #alerts = new RecentSet<string>(ALERT_MEMORY);
  #stopped = false;

  constructor(user: User, client: QueryClient) {
    this.user = user;
    this.#client = client;
    this.live = new LiveClient({
      url: liveUrl(),
      seqStore: sessionSeqStore(user.id),
      userId: user.id,
    });
    this.patcher = new ZonePatcher(
      client,
      { update: updateZone, get: (id) => getZone(id) },
      {
        onConflict: ({ zone, change }) =>
          notify({
            tone: "warning",
            title: "Zone changed in another session",
            body: `Showing the latest version of ${zone.name}.`,
            action: { label: "Apply mine", run: () => this.patcher.patch(zone.id, change) },
          }),
        onGone: () =>
          notify({
            tone: "warning",
            title: "Zone was deleted",
            body: "Another session removed it.",
          }),
        onError: () =>
          notify({
            tone: "error",
            title: "Couldn't save the zone",
            body: "Your change was undone.",
          }),
      },
    );
  }

  start(): void {
    this.#unsubscribe.push(this.live.subscribe((event) => this.#onLive(event)));
    this.#unsubscribe.push(
      this.#client.getQueryCache().subscribe((event) => {
        if (event.query.queryKey[0] === ZONES_KEY[0]) this.#syncZones();
      }),
    );
    mapController.attachFleet(this.fleet, () => this.live.clock.now());
    mapController.onViewport = (viewport) => this.setViewport(viewport);
    this.#timers.push(
      setInterval(() => {
        if (this.fleet.sweepStale(this.live.clock.now(), STALE_S) > 0) mapController.poke();
      }, SWEEP_MS),
      setInterval(() => this.#publishCounters(), COUNTERS_MS),
    );
    this.#syncZones();
    const viewport = mapController.viewport();
    if (viewport) this.setViewport(viewport);
    this.live.start();
  }

  stop(): void {
    this.#stopped = true;
    for (const off of this.#unsubscribe.splice(0)) off();
    for (const timer of this.#timers.splice(0)) clearInterval(timer);
    for (const { timer } of this.#occupantRefresh.values()) clearTimeout(timer);
    this.#occupantRefresh.clear();
    this.live.stop();
    mapController.onViewport = null;
    mapController.detachFleet();
    // The map outlives the session: the signed-out backdrop, or the next account's workspace,
    // must not keep drawing this account's zones.
    mapController.setZones([]);
    this.fleet.clear();
  }

  setViewport(viewport: Viewport): void {
    this.#viewport = viewport;
    this.live.setViewport(viewport);
    this.#retainCoverage();
  }

  /**
   * Drop the devices the stream no longer covers. Only a change of cover can strand any: a device
   * that drives out of it is never heard from again, so it still sits where the cover reaches.
   */
  #retainCoverage(): void {
    const viewport = this.#viewport;
    if (!viewport) return;
    const [west, south, east, north] = viewport.bbox;
    const prefixes = coveringQuadkeys(
      { west, south, east, north },
      MAX_VIEWPORT_TILES,
      this.#tileZoom,
    );
    const cover = prefixes.join(" ");
    if (cover === this.#cover) return;
    this.#cover = cover;
    this.#prefixes = prefixes;
    if (this.fleet.retainCoverage(prefixes) > 0) mapController.poke();
  }

  setOps(on: boolean): void {
    this.live.setOps(on);
  }

  /* ---------------------------------------------------------------- live frames */

  #onLive(event: LiveEvent): void {
    switch (event.type) {
      case "positions":
        this.#onPositions(event.data);
        break;
      case "event":
        useLive.getState().setLastSeq(event.seq);
        this.#onEvent(event.event, event.replayed);
        break;
      case "hello":
        this.#onHello(event.hello);
        break;
      case "reset":
        void this.#client.invalidateQueries({ queryKey: ZONES_KEY });
        void this.#client.invalidateQueries({ queryKey: ["alerts"] });
        break;
      case "pulse":
        this.#onPulse(event.frame.zones);
        break;
      case "sessions":
        this.#client.setQueryData(SESSIONS_KEY, event.sessions);
        break;
      case "ops":
        this.#onOps(event.frame);
        break;
      case "latency":
        useLive.getState().setLatency(event.rttMs, this.live.clock.offsetMs);
        break;
      case "status":
        useLive.getState().setStatus(event.status);
        if (event.status.state === "signedOut") {
          useSession
            .getState()
            .signedOut(
              event.status.expired
                ? "Your session has expired. Sign in again."
                : "This session was signed out from another device.",
            );
        } else if (event.status.state === "blocked" && event.status.code === 4003) {
          void this.#checkIdentity();
        }
        break;
      case "accountChanged":
        void this.#checkIdentity();
        break;
      case "resync":
        break;
      case "protocolError":
        console.warn(`live channel: ${event.message}`);
        break;
    }
  }

  #onHello(hello: HelloFrame): void {
    useLive.getState().setHello({
      sessionId: hello.session_id,
      replica: hello.replica,
      lastSeq: hello.resume.after,
      clockOffsetMs: this.live.clock.offsetMs,
    });
    if (hello.tile_zoom !== this.#tileZoom) {
      this.#tileZoom = hello.tile_zoom;
      this.fleet.setLeafZoom(hello.tile_zoom);
      this.#cover = null;
      this.#retainCoverage();
    }
    if (hello.resume.mode === "replay") {
      // What happened while the tab was away is replayed as events, but the occupancy counts
      // are re-read instead: the list may already include those arrivals and departures (after
      // a reload it was fetched just now), so counting them again would count them twice.
      void this.#client.invalidateQueries({ queryKey: ZONES_KEY }, { cancelRefetch: false });
      void this.#client.invalidateQueries({ queryKey: ["occupants"] }, { cancelRefetch: false });
    }
  }

  #onPositions(data: ArrayBuffer): void {
    let frames: ReturnType<typeof decodeBundleFrames>;
    try {
      frames = decodeBundleFrames(data);
    } catch (error) {
      if (error instanceof FrameError) {
        console.warn(`live channel: dropped malformed position bundle (${error.message})`);
        return;
      }
      throw error;
    }
    const serverNow = this.live.clock.now();
    const prefixes = this.#prefixes;
    for (const frame of frames) {
      // Tiles the view has just left keep arriving until the server has read the new viewport;
      // taking them in would leave devices behind that no update ever reaches again.
      if (prefixes && !coveredBy(quadkey({ x: frame.x, y: frame.y, z: frame.zoom }), prefixes)) {
        continue;
      }
      this.fleet.applyFrame(frame, serverNow);
    }
    mapController.onPositions();
  }

  #onEvent(event: EventEnvelope, replayed: boolean): void {
    // One alert can reach the tab twice under different sequence numbers: when the database is
    // down longer than the broker's de-duplication window, the outbox publishes it again. Its
    // event id (the alert's own id) gives it away.
    if (event.type === "alert" && !this.#alerts.add(event.id)) return;
    if (replayed) useLive.getState().addAway(1);
    if (event.type === "alert") {
      const data = event.data;
      const alert = alertFromRecord({
        id: data.alert_id,
        zone: data.zone,
        device_id: data.device_id,
        kind: data.kind,
        position: data.position,
        occurred_at: data.occurred_at,
      });
      const ui = useUi.getState();
      useAlerts.getState().receive(alert, { replayed, watching: ui.panel === "alerts" });
      // Replayed arrivals and departures are in the counts re-read after the replay (see hello).
      if (replayed || data.kind === "dwell") return;
      bumpOccupancy(this.#client, data.zone.id, data.kind === "enter" ? 1 : -1);
      this.#refreshOccupants(data.zone.id);
      return;
    }
    const change =
      event.type === "zone.deleted"
        ? ({ type: "zone.deleted", id: event.data.id } as const)
        : ({ type: event.type, zone: event.data } as const);
    // The cache write inside would sync the map at once, before it is known whether the change
    // came from elsewhere, and the map would take it without the animation that shows it.
    this.#holdZoneSync = true;
    let changedElsewhere: boolean;
    try {
      changedElsewhere = applyZoneEvent(this.#client, change, this.patcher);
    } finally {
      this.#holdZoneSync = false;
    }
    if (changedElsewhere) {
      this.#animate.add(change.type === "zone.deleted" ? change.id : change.zone.id);
    }
    this.#syncZones();
    if (change.type === "zone.deleted") {
      const selection = useUi.getState().selection;
      if (selection?.kind === "zone" && selection.id === change.id) useUi.getState().select(null);
    }
  }

  #onPulse(zones: Record<string, string[]>): void {
    const now = Date.now();
    for (const [zoneId, devices] of Object.entries(zones)) {
      let seen = this.#reporting.get(zoneId);
      if (!seen) {
        seen = new Map();
        this.#reporting.set(zoneId, seen);
      }
      for (const id of devices) seen.set(id, now);
    }
    mapController.pulse(Object.keys(zones));
  }

  #onOps(frame: OpsFrame): void {
    useLive.getState().pushOps(frame, seriesOf(frame));
  }

  /**
   * Find out who this browser is signed in as now. The session may have ended, or another tab may
   * have signed in as someone else (the session cookie is shared): this tab then follows that
   * account instead of showing its data under the old name.
   */
  async #checkIdentity(): Promise<void> {
    let user: User | null;
    try {
      user = await currentUser();
    } catch {
      // The network is down: the connection state already says so, and the next hello asks again.
      return;
    }
    const session = useSession.getState();
    if (this.#stopped) return;
    if (!user) {
      session.signedOut("Your session has ended. Sign in again.");
    } else if (user.id !== this.user.id && session.user?.id !== user.id) {
      session.signedIn(user);
      notify({
        tone: "info",
        title: `Signed in as ${user.username}`,
        body: "This browser switched accounts in another tab.",
      });
    }
  }

  /* ---------------------------------------------------------------- zones */

  /**
   * Hand the zones to the map and the fleet when something they draw changed. The list changes far
   * more often than that — every arrival and departure moves an occupancy count — and rebuilding
   * every zone layer for a count nobody sees on the map would cost a full re-tile each time.
   */
  #syncZones(): void {
    if (this.#holdZoneSync) return;
    const zones = readZones(this.#client);
    const drawn = zones
      .map(
        (z) =>
          `${z.id}:${z.name}:${z.center.lat}:${z.center.lon}:${z.radius_m}:${z.color}:${z.is_active}`,
      )
      .join("|");
    const animate = this.#animate;
    if (drawn === this.#drawnZones && animate.size === 0) return;
    this.#drawnZones = drawn;
    this.#animate = new Set();
    mapController.setZones(zones, animate);
    const signature = zones
      .map((z) => `${z.id}:${z.center.lat}:${z.center.lon}:${z.radius_m}:${z.color}:${z.is_active}`)
      .join("|");
    if (signature !== this.#tintZones) {
      this.#tintZones = signature;
      this.fleet.setZones(
        zones.map((z: Zone) => ({
          id: z.id,
          lat: z.center.lat,
          lon: z.center.lon,
          radiusM: z.radius_m,
          color: packRgba(hexToRgba(z.color)),
          active: z.is_active,
        })),
      );
      mapController.poke();
    }
  }

  /**
   * Read a zone's occupants again after an arrival or departure: at once, then at most once a
   * second while more keep coming. A read already on its way is let finish rather than restarted,
   * or a steady stream of alerts would never let one complete.
   */
  #refreshOccupants(zoneId: string): void {
    const pending = this.#occupantRefresh.get(zoneId);
    if (pending) {
      pending.again = true;
      return;
    }
    void this.#client.invalidateQueries(
      { queryKey: ["occupants", zoneId] },
      { cancelRefetch: false },
    );
    const entry = {
      again: false,
      timer: setTimeout(() => {
        this.#occupantRefresh.delete(zoneId);
        if (entry.again) this.#refreshOccupants(zoneId);
      }, OCCUPANTS_REFRESH_MS),
    };
    this.#occupantRefresh.set(zoneId, entry);
  }

  /* ---------------------------------------------------------------- counters */

  #publishCounters(): void {
    const live = useLive.getState();
    const view = mapController.viewport();
    if (view) {
      const [west, south, east, north] = view.bbox;
      const { total, moving } = this.fleet.countWithin(west, south, east, north);
      live.setFleet(total, moving);
    }
    const cutoff = Date.now() - REPORTING_WINDOW_MS;
    const reporting: Record<string, number> = {};
    for (const [zoneId, seen] of this.#reporting) {
      for (const [id, at] of seen) if (at < cutoff) seen.delete(id);
      if (seen.size === 0) this.#reporting.delete(zoneId);
      else reporting[zoneId] = seen.size;
    }
    live.setReporting(reporting);
  }
}

let runtime: Runtime | null = null;

export function startRuntime(user: User, client: QueryClient): Runtime {
  runtime?.stop();
  runtime = new Runtime(user, client);
  runtime.start();
  return runtime;
}

export function stopRuntime(): void {
  runtime?.stop();
  runtime = null;
  useLive.getState().reset();
  useAlerts.getState().reset();
}

export function getRuntime(): Runtime | null {
  return runtime;
}
