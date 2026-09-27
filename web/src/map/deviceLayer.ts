/**
 * Devices as a MapLibre custom WebGL2 layer, fed straight from the fleet's instance buffer.
 *
 * One instanced draw call renders the whole fleet: each device is a screen-aligned quad whose
 * fragment shader draws a haloed dot and, at street zoom, a heading chevron. Motion is
 * interpolated on the GPU between the last two reported positions, so between updates the CPU does
 * nothing but set a few uniforms per frame. Positions arrive relative to the fleet's origin and
 * the projection matrix is translated to that origin in float64 before it is narrowed to float32
 * (relative-to-centre rendering), which keeps dots steady to the pixel at any zoom.
 *
 * The frame rate adapts to how fast devices move on screen: at a city-wide zoom a vehicle crosses
 * a pixel every second or so, and repainting sixty times a second would only burn battery.
 */
import type {
  CustomLayerInterface,
  CustomRenderMethodInput,
  Map as MapLibreMap,
} from "maplibre-gl";
import { type FleetStore, foldX, InstanceLayout, STRIDE } from "@/fleet/store";
import { mercatorX, mercatorY } from "@/lib/geodesy";

export interface DeviceStyle {
  moving: [number, number, number, number];
  stationary: [number, number, number, number];
  halo: [number, number, number, number];
  accent: [number, number, number, number];
}

export interface DeviceLayerOptions {
  fleet: FleetStore;
  style: () => DeviceStyle;
  /** Server time now, epoch milliseconds (device ages are measured on the server's clock). */
  serverNow: () => number;
  selected: () => string | null;
  hovered: () => string | null;
  staleAfterS?: number;
}

const VERTEX = `#version 300 es
precision highp float;

layout(location = 0) in vec2 a_corner;
layout(location = 1) in vec2 a_from;
layout(location = 2) in vec2 a_to;
layout(location = 3) in vec2 a_time;
layout(location = 4) in float a_heading;
layout(location = 5) in float a_speed;
layout(location = 6) in float a_recorded;
layout(location = 7) in float a_born;
layout(location = 8) in vec4 a_color;
layout(location = 9) in float a_flags;

uniform mat4 u_matrix;
uniform vec2 u_viewport;
uniform float u_now;
uniform float u_server_now;
uniform float u_radius;
uniform float u_bearing;
uniform float u_chevrons;
uniform float u_px_per_meter;
uniform float u_stale_after;
uniform int u_selected;
uniform int u_hovered;
uniform int u_pass;
uniform vec4 u_moving;
uniform vec4 u_stationary;

out vec2 v_uv;
out vec2 v_local;
out vec4 v_fill;
out float v_alpha;
out float v_chevron;
out float v_tail;
out float v_ring;

// Seconds of travel the motion tail represents.
const float TAIL_SECONDS = 1.6;
const float MAX_TAIL = 7.0;

void main() {
  bool selected = gl_InstanceID == u_selected;
  bool hovered = gl_InstanceID == u_hovered;
  bool highlighted = selected || hovered;
  // Pass 0 draws the fleet, pass 1 redraws highlighted devices on top of it.
  if ((u_pass == 0) == highlighted) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    return;
  }

  float t = a_time.y > 0.0 ? clamp((u_now - a_time.x) / a_time.y, 0.0, 1.0) : 1.0;
  vec4 clip = u_matrix * vec4(mix(a_from, a_to, t), 0.0, 1.0);

  bool moving = mod(a_flags, 2.0) >= 1.0;
  bool heading = a_heading >= 0.0;
  float chevron = (moving && heading) ? u_chevrons : 0.0;
  float ring = selected ? 1.0 : (hovered ? 0.7 : 0.0);
  float scale = selected ? 1.4 : (hovered ? 1.2 : 1.0);
  float radius = u_radius * scale;
  float tail = (moving && heading && a_speed > 0.0)
    ? min(MAX_TAIL, a_speed * TAIL_SECONDS * u_px_per_meter / radius) * u_chevrons
    : 0.0;
  float extent = max(1.45 + 1.4 * chevron, tail + 0.6);
  extent = max(extent, 2.2 * step(0.01, ring));

  vec2 corner = a_corner * extent;
  // Rotate the local frame so +y points along the heading (screen space, north-up corrected).
  float angle = a_heading - u_bearing;
  float c = cos(angle);
  float s = sin(angle);
  v_uv = corner;
  v_local = vec2(c * corner.x - s * corner.y, s * corner.x + c * corner.y);
  v_chevron = chevron;
  v_tail = tail;
  v_ring = ring;

  clip.xy += corner * radius * 2.0 / u_viewport * clip.w;
  gl_Position = clip;

  v_fill = a_color.a > 0.0 ? vec4(a_color.rgb, 1.0) : (moving ? u_moving : u_stationary);
  float age = u_server_now - a_recorded;
  float fresh = 1.0 - 0.68 * smoothstep(u_stale_after - 8.0, u_stale_after, age);
  float born = clamp((u_now - a_born) / 0.4, 0.0, 1.0);
  v_alpha = fresh * born;
}
`;

const FRAGMENT = `#version 300 es
precision highp float;

in vec2 v_uv;
in vec2 v_local;
in vec4 v_fill;
in float v_alpha;
in float v_chevron;
in float v_tail;
in float v_ring;

uniform vec4 u_halo;
uniform vec4 u_accent;

out vec4 fragColor;

// Signed distance to a triangle (Inigo Quilez).
float sdTriangle(vec2 p, vec2 p0, vec2 p1, vec2 p2) {
  vec2 e0 = p1 - p0, e1 = p2 - p1, e2 = p0 - p2;
  vec2 v0 = p - p0, v1 = p - p1, v2 = p - p2;
  vec2 pq0 = v0 - e0 * clamp(dot(v0, e0) / dot(e0, e0), 0.0, 1.0);
  vec2 pq1 = v1 - e1 * clamp(dot(v1, e1) / dot(e1, e1), 0.0, 1.0);
  vec2 pq2 = v2 - e2 * clamp(dot(v2, e2) / dot(e2, e2), 0.0, 1.0);
  float s = sign(e0.x * e2.y - e0.y * e2.x);
  vec2 d = min(min(vec2(dot(pq0, pq0), s * (v0.x * e0.y - v0.y * e0.x)),
                   vec2(dot(pq1, pq1), s * (v1.x * e1.y - v1.y * e1.x))),
                   vec2(dot(pq2, pq2), s * (v2.x * e2.y - v2.y * e2.x)));
  return -sqrt(d.x) * sign(d.y);
}

void main() {
  float r = length(v_uv);
  float aa = max(fwidth(r), 1e-4);

  // The dot, and at street zoom a detached arrowhead ahead of it.
  float d = r - 1.0;
  if (v_chevron > 0.01) {
    float half_width = 0.7 * v_chevron;
    float base = 1.38;
    float arrow = sdTriangle(v_local, vec2(0.0, base + 0.95 * v_chevron),
                             vec2(-half_width, base), vec2(half_width, base));
    d = min(d, arrow);
  }
  float fill = 1.0 - smoothstep(-aa, aa, d);
  float halo = (1.0 - smoothstep(-aa, aa, d - 0.34)) * u_halo.a;

  // Motion tail behind moving devices: tapering, fading with distance.
  float tail = 0.0;
  if (v_tail > 0.0) {
    float behind = -v_local.y;
    float k = clamp(behind / v_tail, 0.0, 1.0);
    float width = mix(0.78, 0.08, k);
    float inside = step(0.0, behind) * (1.0 - smoothstep(v_tail - aa, v_tail, behind));
    tail = (1.0 - smoothstep(-aa, aa, abs(v_local.x) - width)) * inside * pow(1.0 - k, 1.5) * 0.4;
  }

  vec3 rgb = mix(u_halo.rgb, v_fill.rgb, fill);
  float body_alpha = max(halo, fill);
  vec4 body = vec4(rgb * body_alpha, body_alpha);
  vec4 trail = vec4(v_fill.rgb * tail, tail);
  vec4 device = (body + trail * (1.0 - body.a)) * v_alpha;

  float ring_distance = abs(r - 1.9) - 0.2;
  float ring = v_ring * (1.0 - smoothstep(-aa, aa, ring_distance));
  vec4 glow = vec4(u_accent.rgb, 1.0) * ring;
  fragColor = glow + device * (1.0 - ring);
  if (fragColor.a < 0.003) discard;
}
`;

/** Dot radius in CSS pixels by zoom: dust at city scale, pucks at street scale. */
export function dotRadius(zoom: number): number {
  const stops: [number, number][] = [
    [8, 1.2],
    [10, 1.6],
    [12, 2.2],
    [14, 3.2],
    [16, 4.6],
    [18, 6.2],
  ];
  if (zoom <= (stops[0] as [number, number])[0]) return (stops[0] as [number, number])[1];
  for (let i = 1; i < stops.length; i++) {
    const [z1, r1] = stops[i] as [number, number];
    const [z0, r0] = stops[i - 1] as [number, number];
    if (zoom <= z1) return r0 + ((r1 - r0) * (zoom - z0)) / (z1 - z0);
  }
  return (stops.at(-1) as [number, number])[1];
}

/** More copies than this only show up at world zoom with a steep pitch; the nearest are drawn. */
const MAX_WORLD_COPIES = 6;

/**
 * Whole-world offsets at which the fleet must be drawn to fill the visible Mercator x range
 * [`west`, `east`] (unwrapped: a map panned past ±180° goes beyond [0, 1]). Devices lie within
 * half a world of `originX`, so copy `k` spans [originX + k − ½, originX + k + ½]. Nearest to the
 * camera (`centerX`) first, at most {@link MAX_WORLD_COPIES}.
 */
export function worldCopies(
  originX: number,
  west: number,
  east: number,
  centerX: number,
): number[] {
  const first = Math.ceil(west - originX - 0.5) || 0; // never -0
  const last = Math.floor(east - originX + 0.5);
  const copies: number[] = [];
  for (let k = first; k <= last; k++) copies.push(k);
  const home = centerX - originX;
  copies.sort((a, b) => Math.abs(a - home) - Math.abs(b - home));
  return copies.slice(0, MAX_WORLD_COPIES);
}

/** Re-anchor positions when the camera drifts this far (Mercator units, ~400 km). */
const REBASE_DISTANCE = 0.01;
const TYPICAL_MAX_SPEED_MPS = 20;

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("cannot create shader");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`device shader failed to compile: ${log}`);
  }
  return shader;
}

export class DeviceLayer implements CustomLayerInterface {
  readonly id = "devices";
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;

  #options: DeviceLayerOptions;
  #map: MapLibreMap | null = null;
  #program: WebGLProgram | null = null;
  #vao: WebGLVertexArrayObject | null = null;
  #quad: WebGLBuffer | null = null;
  #instances: WebGLBuffer | null = null;
  #uniforms = new Map<string, WebGLUniformLocation | null>();
  #uploadedCapacity = -1;
  #uploadedLayout = -1;
  #matrix = new Float32Array(16);
  #nextFrame: ReturnType<typeof setTimeout> | null = null;
  /** Frames drawn and time spent, for the performance overlay and tests. */
  stats = { frames: 0, lastDrawMs: 0 };

  constructor(options: DeviceLayerOptions) {
    this.#options = options;
  }

  onAdd(map: MapLibreMap, gl: WebGL2RenderingContext): void {
    this.#map = map;
    const program = gl.createProgram();
    if (!program) throw new Error("cannot create program");
    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX);
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT);
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`device program failed to link: ${gl.getProgramInfoLog(program)}`);
    }
    this.#program = program;
    for (const name of [
      "u_matrix",
      "u_viewport",
      "u_now",
      "u_server_now",
      "u_radius",
      "u_bearing",
      "u_chevrons",
      "u_px_per_meter",
      "u_stale_after",
      "u_selected",
      "u_hovered",
      "u_pass",
      "u_moving",
      "u_stationary",
      "u_halo",
      "u_accent",
    ]) {
      this.#uniforms.set(name, gl.getUniformLocation(program, name));
    }

    this.#vao = gl.createVertexArray();
    gl.bindVertexArray(this.#vao);
    this.#quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.#quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    this.#instances = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.#instances);
    const bytes = STRIDE * 4;
    const floatAttr = (location: number, size: number, offset: number) => {
      gl.enableVertexAttribArray(location);
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, bytes, offset * 4);
      gl.vertexAttribDivisor(location, 1);
    };
    floatAttr(1, 2, InstanceLayout.from);
    floatAttr(2, 2, InstanceLayout.to);
    floatAttr(3, 2, InstanceLayout.time);
    floatAttr(4, 1, InstanceLayout.heading);
    floatAttr(5, 1, InstanceLayout.speed);
    floatAttr(6, 1, InstanceLayout.recorded);
    floatAttr(7, 1, InstanceLayout.born);
    gl.enableVertexAttribArray(8);
    gl.vertexAttribPointer(8, 4, gl.UNSIGNED_BYTE, true, bytes, InstanceLayout.color * 4);
    gl.vertexAttribDivisor(8, 1);
    floatAttr(9, 1, InstanceLayout.flags);
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
    this.#uploadedCapacity = -1;
  }

  onRemove(_map: MapLibreMap, gl: WebGL2RenderingContext): void {
    if (this.#nextFrame) clearTimeout(this.#nextFrame);
    this.#nextFrame = null;
    gl.deleteBuffer(this.#quad);
    gl.deleteBuffer(this.#instances);
    gl.deleteVertexArray(this.#vao);
    gl.deleteProgram(this.#program);
    this.#map = null;
  }

  /** Ask for a frame now (selection or hover changed). */
  refresh(): void {
    this.#map?.triggerRepaint();
  }

  /** New data arrived: make sure a frame is coming, without breaking the adaptive frame rate. */
  poke(): void {
    if (!this.#nextFrame) this.#map?.triggerRepaint();
  }

  render(gl: WebGL2RenderingContext, input: CustomRenderMethodInput): void {
    const map = this.#map;
    const program = this.#program;
    if (!map || !program || !this.#vao || !this.#instances) return;
    const started = performance.now();
    const fleet = this.#options.fleet;

    const center = map.getCenter();
    // The camera keeps its longitude unwrapped once the map is panned past ±180°; the origin
    // lives in the first world and the fleet is drawn into every world copy on screen.
    const cx = mercatorX(center.lng);
    const cy = mercatorY(center.lat);
    if (Math.hypot(foldX(cx - fleet.originX), cy - fleet.originY) > REBASE_DISTANCE) {
      fleet.rebase(cx - Math.floor(cx), cy);
    }
    this.#upload(gl, fleet);

    const bounds = map.getBounds();
    const copies = worldCopies(
      fleet.originX,
      mercatorX(bounds.getWest()),
      mercatorX(bounds.getEast()),
      cx,
    );
    const canvas = map.getCanvas();
    const zoom = map.getZoom();
    const style = this.#options.style();
    const u = this.#uniforms;
    const selectedId = this.#options.selected();
    const hoveredId = this.#options.hovered();
    const selected = selectedId === null ? -1 : (fleet.indexOf(selectedId) ?? -1);
    const hovered = hoveredId === null ? -1 : (fleet.indexOf(hoveredId) ?? -1);

    // biome-ignore lint/correctness/useHookAtTopLevel: WebGL call, not a React hook.
    gl.useProgram(program);
    gl.uniform2f(u.get("u_viewport") ?? null, canvas.clientWidth, canvas.clientHeight);
    gl.uniform1f(u.get("u_now") ?? null, fleet.animationTime());
    gl.uniform1f(u.get("u_server_now") ?? null, this.#options.serverNow() / 1000 - fleet.epochBase);
    gl.uniform1f(u.get("u_radius") ?? null, dotRadius(zoom));
    gl.uniform1f(u.get("u_bearing") ?? null, (map.getBearing() * Math.PI) / 180);
    gl.uniform1f(u.get("u_chevrons") ?? null, smoothstep(14.2, 15.4, zoom));
    const metersPerPixel = (78271.517 * Math.cos((center.lat * Math.PI) / 180)) / 2 ** zoom;
    gl.uniform1f(u.get("u_px_per_meter") ?? null, 1 / metersPerPixel);
    gl.uniform1f(u.get("u_stale_after") ?? null, this.#options.staleAfterS ?? 60);
    gl.uniform1i(u.get("u_selected") ?? null, selected);
    gl.uniform1i(u.get("u_hovered") ?? null, hovered === selected ? -1 : hovered);
    gl.uniform4fv(u.get("u_moving") ?? null, style.moving);
    gl.uniform4fv(u.get("u_stationary") ?? null, style.stationary);
    gl.uniform4fv(u.get("u_halo") ?? null, style.halo);
    gl.uniform4fv(u.get("u_accent") ?? null, style.accent);

    gl.bindVertexArray(this.#vao);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    if (fleet.count > 0) {
      const mainMatrix = input.defaultProjectionData.mainMatrix;
      for (const copy of copies) {
        this.#originMatrix(mainMatrix, fleet.originX + copy, fleet.originY);
        gl.uniformMatrix4fv(u.get("u_matrix") ?? null, false, this.#matrix);
        gl.uniform1i(u.get("u_pass") ?? null, 0);
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, fleet.count);
        if (selected >= 0 || hovered >= 0) {
          gl.uniform1i(u.get("u_pass") ?? null, 1);
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, fleet.count);
        }
      }
    }
    gl.bindVertexArray(null);

    this.stats.frames++;
    this.stats.lastDrawMs = performance.now() - started;
    this.#scheduleNext(zoom, center.lat);
  }

  #upload(gl: WebGL2RenderingContext, fleet: FleetStore): void {
    gl.bindBuffer(gl.ARRAY_BUFFER, this.#instances);
    if (this.#uploadedCapacity !== fleet.capacity || this.#uploadedLayout !== fleet.layoutVersion) {
      gl.bufferData(gl.ARRAY_BUFFER, fleet.instances, gl.DYNAMIC_DRAW);
      this.#uploadedCapacity = fleet.capacity;
      this.#uploadedLayout = fleet.layoutVersion;
      fleet.takeDirty();
    } else {
      const range = fleet.takeDirty();
      if (range) {
        const [lo, hi] = range;
        gl.bufferSubData(
          gl.ARRAY_BUFFER,
          lo * STRIDE * 4,
          fleet.instances,
          lo * STRIDE,
          (hi - lo + 1) * STRIDE,
        );
      }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  /** `matrix = projection · translate(origin)`, computed in float64, stored as float32. */
  #originMatrix(m: ArrayLike<number>, ox: number, oy: number): void {
    const out = this.#matrix;
    for (let i = 0; i < 12; i++) out[i] = m[i] as number;
    for (let row = 0; row < 4; row++) {
      out[12 + row] =
        (m[row] as number) * ox + (m[4 + row] as number) * oy + (m[12 + row] as number);
    }
  }

  /**
   * Keep frames coming while devices move, at the rate their on-screen motion needs: sub-pixel
   * motion per frame is invisible, so zoomed-out views repaint less often.
   */
  #scheduleNext(zoom: number, lat: number): void {
    const map = this.#map;
    if (!map || this.#nextFrame || this.#options.fleet.count === 0) return;
    const metersPerPixel = (78271.517 * Math.cos((lat * Math.PI) / 180)) / 2 ** zoom;
    const pixelsPerSecond = TYPICAL_MAX_SPEED_MPS / metersPerPixel;
    // Anything visibly gliding (a few pixels a second) gets every display frame; motion well
    // under a pixel per frame looks identical at a lower rate.
    const interval = pixelsPerSecond > 8 ? 0 : pixelsPerSecond > 2 ? 33 : 66;
    // The requested frame lands on the next display refresh, so wait one refresh less.
    const wait = interval - 16;
    if (wait <= 0) {
      map.triggerRepaint();
      return;
    }
    this.#nextFrame = setTimeout(() => {
      this.#nextFrame = null;
      this.#map?.triggerRepaint();
    }, wait);
  }
}

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}
