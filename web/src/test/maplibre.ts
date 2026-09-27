/**
 * MapLibre as far as the map controller uses it, for jsdom (which has no WebGL). The map keeps a
 * camera and a real canvas element, sources and layers go nowhere, and markers put their elements
 * into the map's container: the controller's own DOM — the canvas it hands the keyboard to, the
 * zone handles it builds — is there to test as a browser would show it.
 *
 * Mock `maplibre-gl` with this module and stub `fetch` (the controller loads the basemap style).
 */
import { latFromMercatorY, lonFromMercatorX, mercatorX, mercatorY } from "@/lib/geodesy";

type Listener = (event?: unknown) => void;

/** The screen size the fake map pretends to have (jsdom lays nothing out). */
const WIDTH = 1280;
const HEIGHT = 800;

class Handler {
  enabled = true;
  enable(): void {
    this.enabled = true;
  }
  disable(): void {
    this.enabled = false;
  }
  isEnabled(): boolean {
    return this.enabled;
  }
}

export class FakeMap {
  /** Every map created, newest last. */
  static created: FakeMap[] = [];
  readonly options: Record<string, unknown>;
  readonly container: HTMLElement;
  readonly canvas = document.createElement("canvas");
  readonly dragPan = new Handler();
  readonly scrollZoom = new Handler();
  readonly boxZoom = new Handler();
  readonly dragRotate = new Handler();
  readonly keyboard = new Handler();
  readonly doubleClickZoom = new Handler();
  readonly touchZoomRotate = new Handler();
  readonly touchPitch = new Handler();
  #listeners = new Map<string, Set<Listener>>();
  #center: { lng: number; lat: number };
  #zoom: number;
  #bearing = 0;

  constructor(options: {
    container: HTMLElement;
    center: [number, number];
    zoom: number;
    [option: string]: unknown;
  }) {
    this.options = options;
    this.container = options.container;
    this.canvas.tabIndex = 0;
    Object.defineProperties(this.canvas, {
      clientWidth: { value: WIDTH },
      clientHeight: { value: HEIGHT },
    });
    this.container.append(this.canvas);
    this.#center = { lng: options.center[0], lat: options.center[1] };
    this.#zoom = options.zoom;
    FakeMap.created.push(this);
    // Loaded once whoever created it has had the chance to listen.
    setTimeout(() => this.#fire("load"), 0);
  }

  on(type: string, listener: Listener): this {
    let listeners = this.#listeners.get(type);
    if (!listeners) {
      listeners = new Set();
      this.#listeners.set(type, listeners);
    }
    listeners.add(listener);
    return this;
  }

  once(type: string, listener: Listener): this {
    const once: Listener = (event) => {
      this.off(type, once);
      listener(event);
    };
    return this.on(type, once);
  }

  off(type: string, listener: Listener): this {
    this.#listeners.get(type)?.delete(listener);
    return this;
  }

  #fire(type: string, event?: unknown): void {
    for (const listener of [...(this.#listeners.get(type) ?? [])]) listener(event);
  }

  loaded(): boolean {
    return true;
  }

  remove(): void {
    this.canvas.remove();
    this.#listeners.clear();
  }

  addControl(): this {
    return this;
  }

  getStyle(): { layers: { id: string; type: string }[] } {
    return { layers: [] };
  }

  addSource(): void {}
  addLayer(): void {}
  removeLayer(): void {}
  getLayer(): undefined {
    return undefined;
  }
  getSource(): undefined {
    return undefined;
  }
  setPaintProperty(): void {}
  setLayoutProperty(): void {}
  setFeatureState(): void {}
  queryRenderedFeatures(): never[] {
    return [];
  }
  triggerRepaint(): void {}

  getCanvas(): HTMLCanvasElement {
    return this.canvas;
  }

  getCanvasContainer(): HTMLElement {
    return this.container;
  }

  getCenter(): { lng: number; lat: number } {
    return { ...this.#center };
  }

  getZoom(): number {
    return this.#zoom;
  }

  getBearing(): number {
    return this.#bearing;
  }

  setBearing(bearing: number): this {
    this.#bearing = bearing;
    return this;
  }

  getPadding(): { top: number; right: number; bottom: number; left: number } {
    return { top: 0, right: 0, bottom: 0, left: 0 };
  }

  getBounds() {
    const [west, north] = this.#at(0, 0);
    const [east, south] = this.#at(WIDTH, HEIGHT);
    return {
      getWest: () => west,
      getSouth: () => south,
      getEast: () => east,
      getNorth: () => north,
    };
  }

  project([lng, lat]: [number, number]): { x: number; y: number } {
    const scale = 512 * 2 ** this.#zoom;
    return {
      x: (mercatorX(lng) - mercatorX(this.#center.lng)) * scale + WIDTH / 2,
      y: (mercatorY(lat) - mercatorY(this.#center.lat)) * scale + HEIGHT / 2,
    };
  }

  unproject([x, y]: [number, number]): { lng: number; lat: number } {
    const [lng, lat] = this.#at(x, y);
    return { lng, lat };
  }

  #at(x: number, y: number): [number, number] {
    const scale = 512 * 2 ** this.#zoom;
    return [
      lonFromMercatorX(mercatorX(this.#center.lng) + (x - WIDTH / 2) / scale),
      latFromMercatorY(mercatorY(this.#center.lat) + (y - HEIGHT / 2) / scale),
    ];
  }

  jumpTo(camera: { center?: [number, number]; zoom?: number }): this {
    if (camera.center) this.#center = { lng: camera.center[0], lat: camera.center[1] };
    if (camera.zoom !== undefined) this.#zoom = camera.zoom;
    this.#fire("move");
    this.#fire("moveend");
    return this;
  }

  easeTo(camera: { center?: [number, number]; zoom?: number }): this {
    return this.jumpTo(camera);
  }

  flyTo(camera: { center?: [number, number]; zoom?: number }): this {
    return this.jumpTo(camera);
  }

  fitBounds(): this {
    return this;
  }
}

export class FakeMarker {
  readonly element: HTMLElement;
  lngLat: [number, number] | null = null;

  constructor(options: { element: HTMLElement }) {
    this.element = options.element;
  }

  setLngLat(lngLat: [number, number]): this {
    this.lngLat = lngLat;
    return this;
  }

  addTo(map: FakeMap): this {
    map.getCanvasContainer().append(this.element);
    return this;
  }

  remove(): this {
    this.element.remove();
    return this;
  }

  getElement(): HTMLElement {
    return this.element;
  }
}

/** The `maplibre-gl` module, faked. */
export const fakeMapLibre = {
  Map: FakeMap,
  Marker: FakeMarker,
  ScaleControl: class {},
  LngLatBounds: class {
    extend(): this {
      return this;
    }
  },
  setWorkerUrl: () => {},
};
