/**
 * The one map of the application and everything that happens on it.
 *
 * React mounts it into a container and forwards state changes (theme, selection, drawing mode);
 * the controller owns MapLibre, the layers, and the pointer interactions — hover and click
 * picking, drawing a zone by press-and-drag, dragging a zone's centre and radius handles, and
 * camera follow — and reports back through a small typed event emitter.
 *
 * MapLibre is loaded on demand so the application shell paints before the ~800 kB renderer has
 * arrived; the page shows the basemap's land colour until then.
 */
import type {
  LngLat,
  Map as MapLibreMap,
  MapMouseEvent,
  Marker,
  StyleSpecification,
} from "maplibre-gl";
import type { Zone } from "@/api/schemas";
import { clampRadius } from "@/features/zones/model";
import type { FleetStore } from "@/fleet/store";
import {
  destination,
  inverse,
  latFromMercatorY,
  lonFromMercatorX,
  mercatorX,
  mercatorY,
  normalizeLon,
} from "@/lib/geodesy";
import type { Viewport } from "@/live/client";
import { BASEMAP_STYLE_URL, offlineStyle, restyle, type Theme, themeChanges } from "./basemap";
import { DeviceLayer, type DeviceStyle, dotRadius } from "./deviceLayer";
import { addTrailLayer, setTrailColor, TrailRenderer, trailWidth } from "./trailLayer";
import {
  addZoneLabelLayer,
  addZoneLayers,
  applyZoneTheme,
  ZONE_FILL_LAYER,
  type ZoneGeometry,
  ZoneRenderer,
} from "./zoneLayers";

export const HOME = { lat: 52.3676, lon: 4.9041, zoom: 11.4 };

type MapLibre = typeof import("maplibre-gl");

export interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface DraftState {
  lat: number;
  lon: number;
  radiusM: number;
  /** Screen position of the pointer, for the floating radius label. */
  x: number;
  y: number;
}

export interface MapEvents {
  ready: undefined;
  hover: { id: string; x: number; y: number } | null;
  select: { kind: "zone" | "device"; id: string } | null;
  draft: DraftState | null;
  drawn: { lat: number; lon: number; radiusM: number };
  drawCancel: undefined;
  zoneEdit: { id: string; center?: { lat: number; lon: number }; radiusM?: number; done: boolean };
  followBreak: undefined;
  pointer: { lat: number; lon: number } | null;
  basemap: "ok" | "offline";
}

type Listener<T> = (payload: T) => void;

class Emitter {
  #listeners = new Map<keyof MapEvents, Set<Listener<never>>>();

  on<K extends keyof MapEvents>(event: K, listener: Listener<MapEvents[K]>): () => void {
    let set = this.#listeners.get(event);
    if (!set) {
      set = new Set();
      this.#listeners.set(event, set);
    }
    set.add(listener as Listener<never>);
    return () => set.delete(listener as Listener<never>);
  }

  emit<K extends keyof MapEvents>(event: K, payload: MapEvents[K]): void {
    for (const listener of this.#listeners.get(event) ?? []) {
      (listener as Listener<MapEvents[K]>)(payload);
    }
  }
}

/** Device colours (premultiplied later in the shader): indigo ink on paper, pale on graphite. */
const DEVICE_STYLES: Record<Theme, DeviceStyle> = {
  light: {
    moving: [0.137, 0.129, 0.239, 1],
    stationary: [0.6, 0.588, 0.69, 1],
    halo: [1, 1, 1, 0.95],
    accent: [0.427, 0.365, 0.988, 1],
  },
  dark: {
    moving: [0.914, 0.906, 0.961, 1],
    stationary: [0.435, 0.424, 0.522, 1],
    halo: [0.071, 0.071, 0.086, 0.92],
    accent: [0.545, 0.498, 1, 1],
  },
};

const ACCENT: Record<Theme, string> = { light: "#6d5dfc", dark: "#8b7fff" };

async function loadBaseStyle(): Promise<StyleSpecification | null> {
  try {
    const response = await fetch(BASEMAP_STYLE_URL, { signal: AbortSignal.timeout(8_000) });
    if (!response.ok) return null;
    const style = (await response.json()) as StyleSpecification;
    return Array.isArray(style.layers) ? style : null;
  } catch {
    return null;
  }
}

function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Room kept around fitted zones, inside the space the chrome leaves. */
const FIT_MARGIN = 56;
/** The signed-out backdrop turns this slowly, redrawn at this pace (motion is sub-pixel per step). */
const DRIFT_DEG_PER_S = 0.375;
const DRIFT_FRAME_MS = 33;

export class MapController {
  readonly events = new Emitter();
  map: MapLibreMap | null = null;
  onViewport: ((viewport: Viewport) => void) | null = null;

  #lib: MapLibre | null = null;
  #styles: Record<Theme, StyleSpecification> | null = null;
  #theme: Theme = "light";
  #zones: ZoneRenderer | null = null;
  #trail: TrailRenderer | null = null;
  #devices: DeviceLayer | null = null;
  #fleet: FleetStore | null = null;
  #serverNow: () => number = Date.now;
  #pendingZones: { zones: readonly Zone[]; animate: ReadonlySet<string> } | null = null;
  #zoneById = new Map<string, Zone>();
  #selection: { kind: "zone" | "device"; id: string } | null = null;
  #hovered: string | null = null;
  #hoverFrame = 0;
  #lastPointer: { x: number; y: number } | null = null;
  #drawing = false;
  #draft: { lat: number; lon: number; pointerId: number } | null = null;
  #gestureEndedAt = Number.NEGATIVE_INFINITY;
  #insets: Insets = { top: 0, right: 0, bottom: 0, left: 0 };
  #cameraFrame = 0;
  /** First basemap place-name layer: devices are drawn just beneath it. */
  #placeLabels: string | undefined;
  #handles: { center: Marker; radius: Marker; zoneId: string; azimuth: number } | null = null;
  #editing: ZoneGeometry | null = null;
  #followId: string | null = null;
  #followFrame = 0;
  #viewportTimer: ReturnType<typeof setTimeout> | null = null;
  #lastViewportAt = 0;
  #interactive = true;
  /** The signed-out backdrop's slow turn: its frame request, and the bearing it started from. */
  #drift: { frame: number; from: number } | null = null;
  #cleanup: (() => void)[] = [];
  #mounting: Promise<void> | null = null;
  #epoch = 0;

  /* ---------------------------------------------------------------- lifecycle */

  mount(container: HTMLElement, theme: Theme): Promise<void> {
    if (this.#mounting) return this.#mounting;
    this.#theme = theme;
    this.#epoch += 1;
    this.#mounting = this.#create(container, this.#epoch);
    return this.#mounting;
  }

  async #create(container: HTMLElement, epoch: number): Promise<void> {
    const [lib, worker, base] = await Promise.all([
      import("maplibre-gl"),
      import("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url"),
      loadBaseStyle(),
    ]);
    // Unmounted while loading (React re-mounts effects in development): leave no map behind.
    if (epoch !== this.#epoch) return;
    lib.setWorkerUrl(worker.default);
    this.#lib = lib;
    this.#styles = base
      ? { light: restyle(base, "light"), dark: restyle(base, "dark") }
      : { light: offlineStyle("light"), dark: offlineStyle("dark") };
    this.events.emit("basemap", base ? "ok" : "offline");

    const map = new lib.Map({
      container,
      style: this.#styles[this.#theme],
      center: [HOME.lon, HOME.lat],
      zoom: HOME.zoom,
      maxPitch: 60,
      attributionControl: { compact: true },
      canvasContextAttributes: { antialias: true },
      // The camera lives in the URL (#map=zoom/lat/lon/bearing/pitch): views survive reloads
      // and can be shared.
      hash: "map",
      fadeDuration: 180,
      dragRotate: true,
      pitchWithRotate: true,
    });
    map.addControl(new lib.ScaleControl({ unit: "metric", maxWidth: 96 }), "bottom-left");
    this.map = map;
    await new Promise<void>((resolve) => map.once("load", () => resolve()));
    if (epoch !== this.#epoch) return;

    // Bottom to top: basemap areas and roads, zones, road and water names, the trail, devices,
    // place names, zone names. Dense traffic must not bury the names that orient the reader, and
    // the user's own zone names win label collisions (symbols higher up are placed first).
    const layers = map.getStyle().layers;
    const firstSymbol = layers.find((l) => l.type === "symbol")?.id;
    const placeLabels = layers.find(
      (l) => l.type === "symbol" && (l.id.startsWith("label_") || l.id === "airport"),
    )?.id;
    this.#placeLabels = placeLabels;
    addZoneLayers(map, this.#theme === "dark", firstSymbol);
    addTrailLayer(map, ACCENT[this.#theme], placeLabels);
    addZoneLabelLayer(map, this.#theme === "dark");
    this.#zones = new ZoneRenderer(map, prefersReducedMotion);
    this.#trail = new TrailRenderer(map);
    if (this.#fleet) this.#addDeviceLayer();
    if (this.#pendingZones) {
      this.setZones(this.#pendingZones.zones, this.#pendingZones.animate);
      this.#pendingZones = null;
    }
    this.#bindInteractions(map);
    this.setInteractive(this.#interactive);
    this.events.emit("ready", undefined);
    this.#emitViewport();
  }

  unmount(): void {
    this.#epoch += 1;
    for (const off of this.#cleanup.splice(0)) off();
    this.#stopDrift(false);
    this.#clearHandles();
    this.#zones?.destroy();
    this.#zones = null;
    this.#trail = null;
    this.#devices = null;
    this.map?.remove();
    this.map = null;
    this.#mounting = null;
  }

  get ready(): boolean {
    return this.#zones !== null;
  }

  /* ---------------------------------------------------------------- data */

  attachFleet(fleet: FleetStore, serverNow: () => number): void {
    this.#fleet = fleet;
    this.#serverNow = serverNow;
    if (this.map?.loaded() || this.#zones) this.#addDeviceLayer();
  }

  detachFleet(): void {
    const map = this.map;
    if (map && this.#devices && map.getLayer(this.#devices.id)) map.removeLayer(this.#devices.id);
    this.#devices = null;
    this.#fleet = null;
    this.#trail?.clear();
    this.#setHovered(null);
  }

  #addDeviceLayer(): void {
    const map = this.map;
    const fleet = this.#fleet;
    if (!map || !fleet || this.#devices || !this.#zones) return;
    this.#devices = new DeviceLayer({
      fleet,
      style: () => DEVICE_STYLES[this.#theme],
      serverNow: () => this.#serverNow(),
      selected: () => (this.#selection?.kind === "device" ? this.#selection.id : null),
      hovered: () => this.#hovered,
      trailEnd: () => this.#trail?.end() ?? null,
      trailWidth,
    });
    map.addLayer(this.#devices, this.#placeLabels ?? "zones-label");
  }

  setZones(zones: readonly Zone[], animate: ReadonlySet<string> = new Set()): void {
    this.#zoneById = new Map(zones.map((z) => [z.id, z]));
    if (!this.#zones) {
      this.#pendingZones = { zones, animate };
      return;
    }
    this.#zones.setZones(zones.map(toGeometry), animate);
    const selection = this.#selection;
    if (selection?.kind === "zone" && !this.#handles?.zoneId) this.#syncHandles();
    if (selection?.kind === "zone" && this.#handles && !this.#editing) this.#placeHandles();
  }

  /** Show unsaved geometry for a zone (slider or input being adjusted), or clear it. */
  previewZone(zone: Zone | null): void {
    this.#zones?.setPreview(zone ? toGeometry(zone) : null);
    if (zone && this.#handles?.zoneId === zone.id) this.#placeHandles(toGeometry(zone));
    if (!zone) this.#placeHandles();
  }

  /** Highlight a zone from outside the map (hovering its row in a list). */
  hoverZone(id: string | null): void {
    this.#zones?.hover(id);
  }

  pulse(zoneIds: string[]): void {
    this.#zones?.pulse(zoneIds);
  }

  onPositions(): void {
    this.#devices?.poke();
    const selection = this.#selection;
    if (selection?.kind === "device" && this.#trail?.deviceId === selection.id) {
      const device = this.#fleet?.viewOf(selection.id);
      if (device) this.#trail.extend(selection.id, device.lon, device.lat);
    }
  }

  poke(): void {
    this.#devices?.poke();
  }

  showTrail(deviceId: string, coordinates: [number, number][]): void {
    this.#trail?.show(deviceId, coordinates);
  }

  clearTrail(): void {
    this.#trail?.clear();
  }

  /* ---------------------------------------------------------------- view state */

  setTheme(theme: Theme): void {
    if (theme === this.#theme) return;
    const map = this.map;
    const styles = this.#styles;
    const previous = this.#theme;
    this.#theme = theme;
    if (!map || !styles) return;
    for (const change of themeChanges(styles[previous], styles[theme])) {
      if (!map.getLayer(change.layer)) continue;
      if (change.kind === "paint") {
        map.setPaintProperty(change.layer, change.property, change.value);
      } else {
        map.setLayoutProperty(change.layer, change.property, change.value);
      }
    }
    applyZoneTheme(map, theme === "dark");
    setTrailColor(map, ACCENT[theme]);
    this.#devices?.refresh();
  }

  /** The signed-out backdrop: no interaction, a slow drift. */
  setInteractive(interactive: boolean): void {
    this.#interactive = interactive;
    const map = this.map;
    if (!map) return;
    const handlers = [
      map.dragPan,
      map.scrollZoom,
      map.boxZoom,
      map.dragRotate,
      map.keyboard,
      map.doubleClickZoom,
      map.touchZoomRotate,
      map.touchPitch,
    ];
    for (const handler of handlers) {
      if (interactive) handler.enable();
      else handler.disable();
    }
    if (interactive) this.#stopDrift(true);
    else this.#startDrift();
  }

  /**
   * Turn the backdrop slowly about its centre. Each step rotates about the centre as it is now:
   * an eased move would keep aiming at the screen point it started from, so a resize meanwhile
   * (the keyboard opening on a phone, browser bars, a rotated screen) would slide the city away.
   */
  #startDrift(): void {
    const map = this.map;
    if (!map || this.#drift || prefersReducedMotion()) return;
    let last = performance.now();
    const step = (now: number) => {
      if (!this.#drift) return;
      if (now - last >= DRIFT_FRAME_MS) {
        map.setBearing(map.getBearing() + ((now - last) / 1000) * DRIFT_DEG_PER_S);
        last = now;
      }
      this.#drift.frame = requestAnimationFrame(step);
    };
    this.#drift = { frame: requestAnimationFrame(step), from: map.getBearing() };
  }

  /** Stop turning; `settle` eases back to the bearing the backdrop started from. */
  #stopDrift(settle: boolean): void {
    const drift = this.#drift;
    if (!drift) return;
    cancelAnimationFrame(drift.frame);
    this.#drift = null;
    if (settle) {
      this.map?.easeTo({ bearing: drift.from, duration: prefersReducedMotion() ? 0 : 700 });
    }
  }

  select(selection: { kind: "zone" | "device"; id: string } | null): void {
    const previous = this.#selection;
    this.#selection = selection;
    this.#zones?.select(selection?.kind === "zone" ? selection.id : null);
    if (previous?.kind === "device" && selection?.id !== previous.id) this.#trail?.clear();
    this.#syncHandles();
    this.#devices?.refresh();
  }

  follow(deviceId: string | null): void {
    this.#followId = deviceId;
    cancelAnimationFrame(this.#followFrame);
    this.#followFrame = 0;
    if (deviceId) this.#followTick();
  }

  #followTick = (): void => {
    const map = this.map;
    const fleet = this.#fleet;
    const id = this.#followId;
    if (!map || !fleet || !id) return;
    const index = fleet.indexOf(id);
    if (index !== undefined) {
      const [x, y] = fleet.positionAt(index);
      const center: [number, number] = [this.#nearCamera(lonFromMercatorX(x)), latFromMercatorY(y)];
      map.jumpTo({ center, padding: this.#padding(0) });
    }
    this.#followFrame = requestAnimationFrame(this.#followTick);
  };

  /**
   * Screen space covered by floating panels, sheets and inspectors. Camera moves frame their
   * target in the uncovered part of the map, so nothing lands behind the chrome.
   */
  setInsets(insets: Partial<Insets>): void {
    this.#insets = { ...this.#insets, ...insets };
  }

  #padding(extra: number): Insets {
    const { top, right, bottom, left } = this.#insets;
    return { top: top + extra, right: right + extra, bottom: bottom + extra, left: left + extra };
  }

  /**
   * Run a camera move on the next frame. A click that selects something and moves the camera
   * also opens an inspector; by the next frame its insets are known, so the target is framed in
   * the space that is actually left.
   */
  #camera(move: (map: MapLibreMap) => void): void {
    cancelAnimationFrame(this.#cameraFrame);
    this.#cameraFrame = requestAnimationFrame(() => {
      if (this.map) move(this.map);
    });
  }

  flyTo(lat: number, lon: number, zoom?: number): void {
    this.#camera((map) => {
      const target = {
        center: [lon, lat] as [number, number],
        zoom: zoom ?? Math.max(map.getZoom(), 14),
        padding: this.#padding(0),
      };
      if (prefersReducedMotion()) map.jumpTo(target);
      else map.flyTo({ ...target, speed: 1.6, curve: 1.3, essential: true });
    });
  }

  fitZones(zones: readonly Zone[]): void {
    this.#camera(() => this.#fitZones(zones));
  }

  #fitZones(zones: readonly Zone[]): void {
    const map = this.map;
    const lib = this.#lib;
    if (!map || !lib || zones.length === 0) return;
    const bounds = new lib.LngLatBounds();
    for (const z of zones) {
      for (const azimuth of [0, 90, 180, 270]) {
        const p = destination({ lat: z.center.lat, lon: z.center.lon }, azimuth, z.radius_m);
        bounds.extend([p.lon, p.lat]);
      }
    }
    this.#settlePadding(map);
    map.fitBounds(bounds, {
      padding: FIT_MARGIN,
      maxZoom: 16,
      duration: prefersReducedMotion() ? 0 : 900,
    });
  }

  /**
   * Make the camera's own padding the current chrome insets, without moving the view. MapLibre
   * keeps the padding of the last move that set one and `fitBounds` adds its own on top, so a
   * stale value (an inspector that has closed since, say) would push every fit off-centre.
   */
  #settlePadding(map: MapLibreMap): void {
    const insets = this.#padding(0);
    const current = map.getPadding();
    if (
      current.top === insets.top &&
      current.right === insets.right &&
      current.bottom === insets.bottom &&
      current.left === insets.left
    ) {
      return;
    }
    // The point the new padding centres the camera on, taken where the map shows it right now.
    const canvas = map.getCanvas();
    const x = insets.left + (canvas.clientWidth - insets.left - insets.right) / 2;
    const y = insets.top + (canvas.clientHeight - insets.top - insets.bottom) / 2;
    map.jumpTo({ center: map.unproject([x, y]), padding: insets });
  }

  zoomBy(delta: number): void {
    const map = this.map;
    if (!map) return;
    map.easeTo({ zoom: map.getZoom() + delta, duration: prefersReducedMotion() ? 0 : 280 });
  }

  resetNorth(): void {
    this.map?.easeTo({ bearing: 0, pitch: 0, duration: prefersReducedMotion() ? 0 : 400 });
  }

  bearing(): number {
    return this.map?.getBearing() ?? 0;
  }

  viewport(): Viewport | null {
    const map = this.map;
    if (!map) return null;
    const b = map.getBounds();
    return { bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: map.getZoom() };
  }

  /** Stats for the performance readout. */
  deviceLayerStats(): { frames: number; lastDrawMs: number } | null {
    return this.#devices?.stats ?? null;
  }

  /* ---------------------------------------------------------------- drawing */

  setDrawing(drawing: boolean): void {
    this.#drawing = drawing;
    const map = this.map;
    if (!map) return;
    map.getCanvas().style.cursor = drawing ? "crosshair" : "";
    if (drawing) {
      map.dragPan.disable();
      this.#setHovered(null);
    } else {
      if (this.#interactive) map.dragPan.enable();
      this.#draft = null;
      this.#zones?.setDraft(null);
      this.events.emit("draft", null);
    }
  }

  #bindInteractions(map: MapLibreMap): void {
    const canvas = map.getCanvas();

    const onMove = (e: MapMouseEvent) => {
      this.events.emit("pointer", { lat: e.lngLat.lat, lon: normalizeLon(e.lngLat.lng) });
      // Over a zone handle, the handle is what the pointer is on, not the device beneath it.
      this.#lastPointer = e.originalEvent.target === canvas ? { x: e.point.x, y: e.point.y } : null;
      if (this.#drawing || this.#editing) return;
      if (!this.#lastPointer) {
        this.#setHovered(null);
        return;
      }
      if (!this.#hoverFrame) {
        this.#hoverFrame = requestAnimationFrame(() => {
          this.#hoverFrame = 0;
          this.#updateHover();
        });
      }
    };
    const onLeave = () => {
      this.#lastPointer = null;
      this.events.emit("pointer", null);
      this.#setHovered(null);
      this.#zones?.hover(null);
    };
    const onClick = (e: MapMouseEvent) => {
      // The click that ends a drawing or handle drag belongs to that gesture, not to selection.
      if (this.#drawing || performance.now() - this.#gestureEndedAt < 500) return;
      const device = this.#pickDevice(e.point.x, e.point.y);
      if (device) {
        this.events.emit("select", { kind: "device", id: device });
        return;
      }
      const zone = this.#pickZone(e.point.x, e.point.y);
      this.events.emit("select", zone ? { kind: "zone", id: zone } : null);
    };
    map.on("mousemove", onMove);
    map.on("click", onClick);
    canvas.addEventListener("mouseleave", onLeave);

    const onDragStart = () => {
      if (this.#followId) this.events.emit("followBreak", undefined);
    };
    map.on("dragstart", onDragStart);
    map.on("wheel", onDragStart);

    const emitSoon = () => {
      const now = performance.now();
      if (now - this.#lastViewportAt > 250) {
        this.#emitViewport();
        return;
      }
      if (this.#viewportTimer) return;
      this.#viewportTimer = setTimeout(() => {
        this.#viewportTimer = null;
        this.#emitViewport();
      }, 250);
    };
    map.on("move", emitSoon);
    map.on("moveend", () => this.#emitViewport());

    // Drawing: press at the centre, drag out the radius, release to create.
    const onPointerDown = (e: PointerEvent) => {
      if (!this.#drawing || this.#draft || (e.pointerType === "mouse" && e.button !== 0)) return;
      if (!e.isPrimary) return;
      const lngLat = this.#lngLatAt(e);
      if (!lngLat) return;
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      this.#draft = { lat: lngLat.lat, lon: lngLat.lng, pointerId: e.pointerId };
      this.#updateDraft(e);
    };
    const onPointerMove = (e: PointerEvent) => {
      if (!this.#draft || e.pointerId !== this.#draft.pointerId) return;
      this.#updateDraft(e);
    };
    const onPointerUp = (e: PointerEvent) => {
      const draft = this.#draft;
      if (!draft || e.pointerId !== draft.pointerId) return;
      const state = this.#updateDraft(e);
      this.#draft = null;
      this.#zones?.setDraft(null);
      this.events.emit("draft", null);
      if (!state) return;
      const rect = canvas.getBoundingClientRect();
      const start = map.project([draft.lon, draft.lat]);
      const dragged = Math.hypot(e.clientX - rect.left - start.x, e.clientY - rect.top - start.y);
      // A click without a drag drops a zone of a sensible size for the current zoom.
      const radiusM = dragged < 8 ? this.#defaultRadius() : state.radiusM;
      this.#gestureEndedAt = performance.now();
      this.events.emit("drawn", {
        lat: draft.lat,
        lon: normalizeLon(draft.lon),
        radiusM: clampRadius(radiusM),
      });
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && this.#drawing) {
        this.#draft = null;
        this.#zones?.setDraft(null);
        this.events.emit("draft", null);
        this.events.emit("drawCancel", undefined);
      }
    };
    canvas.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    window.addEventListener("keydown", onKey);
    this.#cleanup.push(() => {
      canvas.removeEventListener("mouseleave", onLeave);
      canvas.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
      window.removeEventListener("keydown", onKey);
    });
  }

  /**
   * `lon` moved by whole turns into the world copy the camera looks at: MapLibre keeps the
   * camera's longitude unwrapped once the map is panned past ±180°.
   */
  #nearCamera(lon: number): number {
    const center = this.map?.getCenter().lng ?? lon;
    return lon + 360 * Math.round((center - lon) / 360);
  }

  #lngLatAt(e: PointerEvent): LngLat | null {
    const map = this.map;
    if (!map) return null;
    const rect = map.getCanvas().getBoundingClientRect();
    return map.unproject([e.clientX - rect.left, e.clientY - rect.top]);
  }

  #updateDraft(e: PointerEvent): DraftState | null {
    const draft = this.#draft;
    const lngLat = this.#lngLatAt(e);
    const map = this.map;
    if (!draft || !lngLat || !map) return null;
    const radiusM = clampRadius(
      inverse({ lat: draft.lat, lon: draft.lon }, { lat: lngLat.lat, lon: lngLat.lng }).distanceM,
    );
    // The draft keeps the camera's world copy for screen maths; the zone itself lives in
    // [-180, 180) like every zone (the map repeats it in every copy).
    const lon = normalizeLon(draft.lon);
    this.#zones?.setDraft({
      lat: draft.lat,
      lon,
      radiusM,
      edge: [lngLat.lng + (lon - draft.lon), lngLat.lat],
    });
    const rect = map.getCanvas().getBoundingClientRect();
    const state: DraftState = {
      lat: draft.lat,
      lon,
      radiusM,
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
    };
    this.events.emit("draft", state);
    return state;
  }

  #defaultRadius(): number {
    const map = this.map;
    if (!map) return 300;
    const lat = map.getCenter().lat;
    const metersPerPixel = (78271.517 * Math.cos((lat * Math.PI) / 180)) / 2 ** map.getZoom();
    const meters = metersPerPixel * 70;
    const magnitude = 10 ** Math.floor(Math.log10(meters));
    return Math.round(meters / magnitude) * magnitude;
  }

  /* ---------------------------------------------------------------- picking */

  #pickDevice(x: number, y: number): string | null {
    const map = this.map;
    const fleet = this.#fleet;
    if (!map || !fleet || fleet.count === 0) return null;
    const lngLat = map.unproject([x, y]);
    const zoom = map.getZoom();
    const radiusPx = Math.max(9, dotRadius(zoom) * 2.4);
    const radius = radiusPx / (512 * 2 ** zoom);
    const index = fleet.pick(mercatorX(lngLat.lng), mercatorY(lngLat.lat), radius);
    return index === null ? null : (fleet.ids[index] ?? null);
  }

  #pickZone(x: number, y: number): string | null {
    const map = this.map;
    if (!map?.getLayer(ZONE_FILL_LAYER)) return null;
    const hits = map.queryRenderedFeatures([x, y], { layers: [ZONE_FILL_LAYER] });
    let best: Zone | null = null;
    for (const hit of hits) {
      const zone = this.#zoneById.get(String(hit.properties.id));
      if (zone && (!best || zone.radius_m < best.radius_m)) best = zone;
    }
    return best?.id ?? null;
  }

  #updateHover(): void {
    const pointer = this.#lastPointer;
    const map = this.map;
    if (!pointer || !map) return;
    const device = this.#pickDevice(pointer.x, pointer.y);
    this.#setHovered(device, pointer);
    const zone = device ? null : this.#pickZone(pointer.x, pointer.y);
    this.#zones?.hover(zone);
    map.getCanvas().style.cursor = device || zone ? "pointer" : "";
  }

  #setHovered(id: string | null, pointer?: { x: number; y: number }): void {
    const changed = id !== this.#hovered;
    this.#hovered = id;
    if (changed) this.#devices?.refresh();
    this.events.emit("hover", id && pointer ? { id, x: pointer.x, y: pointer.y } : null);
  }

  /* ---------------------------------------------------------------- zone handles */

  #syncHandles(): void {
    const selection = this.#selection;
    const zone = selection?.kind === "zone" ? this.#zoneById.get(selection.id) : undefined;
    if (!zone || !this.map || !this.#lib) {
      this.#clearHandles();
      return;
    }
    if (this.#handles?.zoneId === zone.id) {
      this.#placeHandles();
      return;
    }
    this.#clearHandles();
    const lib = this.#lib;
    const centerElement = handleElement("center", zone.color);
    const radiusElement = handleElement("radius", zone.color);
    const center = new lib.Marker({ element: centerElement })
      .setLngLat([zone.center.lon, zone.center.lat])
      .addTo(this.map);
    const rim = destination({ lat: zone.center.lat, lon: zone.center.lon }, 90, zone.radius_m);
    const radius = new lib.Marker({ element: radiusElement })
      .setLngLat([rim.lon, rim.lat])
      .addTo(this.map);
    this.#handles = { center, radius, zoneId: zone.id, azimuth: 90 };
    this.#bindHandle(centerElement, zone.id, "center");
    this.#bindHandle(radiusElement, zone.id, "radius");
  }

  /**
   * Drag a handle with pointer capture: the gesture keeps its pointer even when it passes over a
   * panel or leaves the window, and it ends exactly once (release or cancel) — neither of which
   * the map's own marker dragging guarantees once the pointer is over other elements.
   */
  #bindHandle(element: HTMLElement, zoneId: string, kind: "center" | "radius"): void {
    let pointerId: number | null = null;
    const finish = (commit: boolean) => {
      if (pointerId === null) return;
      if (element.hasPointerCapture(pointerId)) element.releasePointerCapture(pointerId);
      pointerId = null;
      this.#gestureEndedAt = performance.now();
      element.classList.remove("zone-handle--dragging");
      if (this.#interactive) this.map?.dragPan.enable();
      const editing = this.#editing;
      this.#editing = null;
      this.#zones?.setPreview(null);
      if (!editing) return;
      if (!commit) {
        this.#placeHandles();
        this.events.emit("zoneEdit", { id: zoneId, done: true });
        return;
      }
      this.events.emit(
        "zoneEdit",
        kind === "center"
          ? { id: zoneId, center: { lat: editing.lat, lon: editing.lon }, done: true }
          : { id: zoneId, radiusM: editing.radiusM, done: true },
      );
      this.#placeHandles(editing);
    };
    element.addEventListener("pointerdown", (e) => {
      if (pointerId !== null || (e.pointerType === "mouse" && e.button !== 0)) return;
      const zone = this.#zoneById.get(zoneId);
      if (!zone) return;
      e.preventDefault();
      e.stopPropagation();
      pointerId = e.pointerId;
      element.setPointerCapture(e.pointerId);
      element.classList.add("zone-handle--dragging");
      this.#setHovered(null);
      this.map?.dragPan.disable();
      this.#editing = toGeometry(zone);
    });
    element.addEventListener("pointermove", (e) => {
      const editing = this.#editing;
      const handles = this.#handles;
      if (e.pointerId !== pointerId || !editing || !handles) return;
      const p = this.#lngLatAt(e);
      if (!p) return;
      if (kind === "center") {
        const lon = normalizeLon(p.lng);
        this.#editing = { ...editing, lat: p.lat, lon };
        this.events.emit("zoneEdit", { id: zoneId, center: { lat: p.lat, lon }, done: false });
      } else {
        const solved = inverse({ lat: editing.lat, lon: editing.lon }, { lat: p.lat, lon: p.lng });
        handles.azimuth = solved.azimuthDeg;
        this.#editing = { ...editing, radiusM: clampRadius(solved.distanceM) };
        this.events.emit("zoneEdit", { id: zoneId, radiusM: this.#editing.radiusM, done: false });
      }
      this.#zones?.setPreview(this.#editing);
      this.#placeHandles(this.#editing);
    });
    element.addEventListener("pointerup", (e) => {
      if (e.pointerId === pointerId) finish(true);
    });
    element.addEventListener("pointercancel", (e) => {
      if (e.pointerId === pointerId) finish(false);
    });
    element.addEventListener("lostpointercapture", (e) => {
      if (e.pointerId === pointerId) finish(true);
    });
  }

  #placeHandles(geometry?: ZoneGeometry): void {
    const handles = this.#handles;
    if (!handles) return;
    const zone =
      geometry ??
      (this.#zoneById.get(handles.zoneId) &&
        toGeometry(this.#zoneById.get(handles.zoneId) as Zone));
    if (!zone) return;
    handles.center.setLngLat([zone.lon, zone.lat]);
    const rim = destination({ lat: zone.lat, lon: zone.lon }, handles.azimuth, zone.radiusM);
    handles.radius.setLngLat([rim.lon, rim.lat]);
    for (const marker of [handles.center, handles.radius]) {
      marker.getElement().style.setProperty("--handle-color", zone.color);
    }
  }

  #clearHandles(): void {
    this.#handles?.center.remove();
    this.#handles?.radius.remove();
    this.#handles = null;
    this.#editing = null;
  }

  #emitViewport(): void {
    this.#lastViewportAt = performance.now();
    if (this.#viewportTimer) {
      clearTimeout(this.#viewportTimer);
      this.#viewportTimer = null;
    }
    const viewport = this.viewport();
    if (viewport) this.onViewport?.(viewport);
  }
}

function toGeometry(zone: Zone): ZoneGeometry {
  return {
    id: zone.id,
    name: zone.name,
    color: zone.color,
    lat: zone.center.lat,
    lon: zone.center.lon,
    radiusM: zone.radius_m,
    active: zone.is_active,
  };
}

function handleElement(kind: "center" | "radius", color: string): HTMLElement {
  const element = document.createElement("div");
  element.className = `zone-handle zone-handle--${kind}`;
  element.style.setProperty("--handle-color", color);
  element.setAttribute("aria-hidden", "true");
  element.title = kind === "center" ? "Drag to move the zone" : "Drag to resize the zone";
  return element;
}

export const mapController = new MapController();
