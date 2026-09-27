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
import { coveringQuadkeys } from "@/lib/tiles";
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

export class Runtime {
  readonly user: User;
  readonly fleet = new FleetStore({ capacity: 16_384 });
  readonly live: LiveClient;
  readonly patcher: ZonePatcher;
  #client: QueryClient;
  #timers: ReturnType<typeof setInterval>[] = [];
  #unsubscribe: (() => void)[] = [];
  #tileZoom = 12;
  #animate = new Set<string>();
  #zoneSignature = "";
  #reporting = new Map<string, Map<string, number>>();

  constructor(user: User, client: QueryClient) {
    this.user = user;
    this.#client = client;
    this.live = new LiveClient({ url: liveUrl(), seqStore: sessionSeqStore(user.id) });
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
    for (const off of this.#unsubscribe.splice(0)) off();
    for (const timer of this.#timers.splice(0)) clearInterval(timer);
    this.live.stop();
    mapController.onViewport = null;
    mapController.detachFleet();
    this.fleet.clear();
  }

  setViewport(viewport: Viewport): void {
    this.live.setViewport(viewport);
    const [west, south, east, north] = viewport.bbox;
    const prefixes = coveringQuadkeys(
      { west, south, east, north },
      MAX_VIEWPORT_TILES,
      this.#tileZoom,
    );
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
        useLive.getState().setLatency(event.rttMs);
        break;
      case "status":
        useLive.getState().setStatus(event.status);
        if (event.status.state === "signedOut") {
          useSession.getState().signedOut("This session was signed out from another device.");
        } else if (event.status.state === "blocked" && event.status.code === 4003) {
          void this.#checkIdentity();
        }
        break;
      case "resync":
        break;
      case "protocolError":
        console.warn(`live channel: ${event.message}`);
        break;
    }
  }

  #onHello(hello: HelloFrame): void {
    useLive.getState().setHello(hello.session_id, hello.replica);
    if (hello.tile_zoom !== this.#tileZoom) {
      this.#tileZoom = hello.tile_zoom;
      this.fleet.setLeafZoom(hello.tile_zoom);
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
    for (const frame of frames) this.fleet.applyFrame(frame, serverNow);
    mapController.onPositions();
  }

  #onEvent(event: EventEnvelope, replayed: boolean): void {
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
      if (data.kind === "enter") bumpOccupancy(this.#client, data.zone.id, 1);
      if (data.kind === "exit") bumpOccupancy(this.#client, data.zone.id, -1);
      if (data.kind !== "dwell") {
        void this.#client.invalidateQueries({ queryKey: ["occupants", data.zone.id] });
      }
      return;
    }
    const change =
      event.type === "zone.deleted"
        ? ({ type: "zone.deleted", id: event.data.id } as const)
        : ({ type: event.type, zone: event.data } as const);
    const changedElsewhere = applyZoneEvent(this.#client, change, (id) => this.patcher.overlay(id));
    if (changedElsewhere) {
      this.#animate.add(change.type === "zone.deleted" ? change.id : change.zone.id);
      this.#syncZones();
    }
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

  async #checkIdentity(): Promise<void> {
    try {
      const user = await currentUser();
      if (!user) useSession.getState().signedOut("Your session has ended. Sign in again.");
    } catch {
      // The network is down; the connection state already says so.
    }
  }

  /* ---------------------------------------------------------------- zones */

  #syncZones(): void {
    const zones = readZones(this.#client);
    const signature = zones
      .map((z) => `${z.id}:${z.center.lat}:${z.center.lon}:${z.radius_m}:${z.color}:${z.is_active}`)
      .join("|");
    const animate = this.#animate;
    this.#animate = new Set();
    mapController.setZones(zones, animate);
    if (signature !== this.#zoneSignature) {
      this.#zoneSignature = signature;
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
