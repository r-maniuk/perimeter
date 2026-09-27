/** Typed calls for every REST resource the dashboard uses (spec §10). */
import { ContractError, isApiError, request, requestEmpty } from "./http";
import {
  type AlertKind,
  AlertPageSchema,
  type AlertRecord,
  DeviceFeatureSchema,
  type LiveSession,
  type Occupant,
  OccupantListSchema,
  SessionListSchema,
  SessionSchema,
  TrailSchema,
  type User,
  UserSchema,
  type Zone,
  ZonePageSchema,
  ZoneSchema,
} from "./schemas";

/* ------------------------------------------------------------------ identity */

export async function signIn(username: string) {
  const { data } = await request("/v1/session", SessionSchema, {
    method: "POST",
    body: { username },
    quietUnauthorized: true,
  });
  return data;
}

export async function signOut(): Promise<void> {
  await requestEmpty("/v1/session", { method: "DELETE", quietUnauthorized: true });
}

/** The signed-in user, or `null` when the session cookie is missing, expired or revoked. */
export async function currentUser(signal?: AbortSignal): Promise<User | null> {
  try {
    const { data } = await request("/v1/me", UserSchema, { signal, quietUnauthorized: true });
    return data;
  } catch (error) {
    if (isApiError(error, 401)) return null;
    throw error;
  }
}

/* ------------------------------------------------------------------ zones */

export interface ZoneInput {
  name: string;
  center: { lat: number; lon: number };
  radius_m: number;
  color: string;
  is_active: boolean;
  notify_enter: boolean;
  notify_exit: boolean;
  dwell_s: number | null;
}

export type ZonePatch = Partial<ZoneInput>;

const PAGE_LIMIT = 100;
/** A user with more zones than this is outside what the dashboard is designed to draw at once. */
const MAX_ZONE_PAGES = 50;

export async function listZones(signal?: AbortSignal): Promise<Zone[]> {
  const zones: Zone[] = [];
  let cursor: string | null | undefined = null;
  for (let page = 0; page < MAX_ZONE_PAGES; page++) {
    const query = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (cursor) query.set("cursor", cursor);
    const { data } = await request(`/v1/geozones?${query}`, ZonePageSchema, { signal });
    zones.push(...data.items);
    cursor = data.next_cursor;
    if (!cursor) break;
  }
  return zones;
}

export async function getZone(id: string, signal?: AbortSignal): Promise<Zone> {
  const { data } = await request(`/v1/geozones/${encodeURIComponent(id)}`, ZoneSchema, { signal });
  return data;
}

export async function createZone(input: ZoneInput): Promise<Zone> {
  const { data } = await request("/v1/geozones", ZoneSchema, { method: "POST", body: input });
  return data;
}

export const etagFor = (version: number) => `"v${version}"`;

export async function updateZone(id: string, patch: ZonePatch, version: number): Promise<Zone> {
  const { data } = await request(`/v1/geozones/${encodeURIComponent(id)}`, ZoneSchema, {
    method: "PATCH",
    body: patch,
    headers: { "if-match": etagFor(version) },
  });
  return data;
}

export async function deleteZone(id: string, version?: number): Promise<void> {
  await requestEmpty(`/v1/geozones/${encodeURIComponent(id)}`, {
    method: "DELETE",
    ...(version === undefined ? {} : { headers: { "if-match": etagFor(version) } }),
  });
}

export interface ZoneOccupants {
  /** Devices inside the zone right now; `items` may list fewer (the list is capped). */
  occupancy: number;
  items: Occupant[];
}

export async function zoneOccupants(id: string, signal?: AbortSignal): Promise<ZoneOccupants> {
  const { data } = await request(
    `/v1/geozones/${encodeURIComponent(id)}/occupants`,
    OccupantListSchema,
    { signal },
  );
  return { occupancy: data.occupancy, items: data.items };
}

/* ------------------------------------------------------------------ alerts */

export interface Alert {
  id: string;
  kind: AlertKind;
  deviceId: string;
  zoneId: string | null;
  zoneName: string;
  lat: number;
  lon: number;
  /** Epoch milliseconds (event time of the report that caused it). */
  occurredAt: number;
}

export function alertFromRecord(record: AlertRecord): Alert {
  return {
    id: record.id,
    kind: record.kind,
    deviceId: record.device_id,
    zoneId: record.zone.id,
    zoneName: record.zone.name,
    lat: record.position.lat,
    lon: record.position.lon,
    occurredAt: Date.parse(record.occurred_at),
  };
}

export interface AlertQuery {
  zoneId?: string | undefined;
  kind?: AlertKind | undefined;
  deviceId?: string | undefined;
  since?: string | undefined;
  cursor?: string | null | undefined;
  limit?: number;
}

export async function listAlerts(query: AlertQuery, signal?: AbortSignal) {
  const params = new URLSearchParams({ limit: String(query.limit ?? 50) });
  if (query.zoneId) params.set("zone_id", query.zoneId);
  if (query.kind) params.set("kind", query.kind);
  if (query.deviceId) params.set("device_id", query.deviceId);
  if (query.since) params.set("since", query.since);
  if (query.cursor) params.set("cursor", query.cursor);
  const { data } = await request(`/v1/alerts?${params}`, AlertPageSchema, { signal });
  return { items: data.items.map(alertFromRecord), nextCursor: data.next_cursor ?? null };
}

/* ------------------------------------------------------------------ devices */

export interface DeviceState {
  id: string;
  lat: number;
  lon: number;
  recordedAt: number | null;
  speedMps: number | null;
  headingDeg: number | null;
  accuracyM: number | null;
}

export async function getDevice(id: string, signal?: AbortSignal): Promise<DeviceState> {
  const { data } = await request(`/v1/devices/${encodeURIComponent(id)}`, DeviceFeatureSchema, {
    signal,
  });
  const [lon, lat] = data.geometry.coordinates;
  const p = data.properties;
  return {
    id: p.device_id ?? String(data.id ?? id),
    lat,
    lon,
    recordedAt: p.recorded_at ? Date.parse(p.recorded_at) : null,
    speedMps: p.speed_mps ?? null,
    headingDeg: p.heading_deg ?? null,
    accuracyM: p.accuracy_m ?? null,
  };
}

export interface Trail {
  /** `[lon, lat]` pairs, oldest first. */
  coordinates: [number, number][];
  /** Device time of each coordinate, epoch milliseconds. */
  times: number[];
  /** False when the server capped the trail (too many points, or a read deadline). */
  complete: boolean;
}

export async function deviceTrail(
  id: string,
  minutes: number,
  signal?: AbortSignal,
): Promise<Trail> {
  const { data } = await request(
    `/v1/devices/${encodeURIComponent(id)}/trail?minutes=${minutes}`,
    TrailSchema,
    { signal, timeoutMs: 8_000 },
  );
  const geometry = data.geometry;
  // Two or more reports make a line, one a point, none no geometry at all.
  const coordinates: [number, number][] =
    geometry === null
      ? []
      : geometry.type === "Point"
        ? [geometry.coordinates]
        : geometry.coordinates;
  const stamps = data.properties.timestamps;
  if (stamps.length !== coordinates.length) {
    throw new ContractError(
      `trail of ${id}: ${stamps.length} timestamps for ${coordinates.length} coordinates`,
    );
  }
  return {
    coordinates,
    times: stamps.map((t) => Date.parse(t)),
    complete: data.properties.complete,
  };
}

/* ------------------------------------------------------------------ sessions */

export async function listSessions(signal?: AbortSignal): Promise<LiveSession[]> {
  const { data } = await request("/v1/sessions", SessionListSchema, { signal });
  return data.sessions;
}

/**
 * Sign a session out remotely. That revokes its sign-in, which closes every socket opened with it,
 * so a sibling socket may already be gone by the time its own request arrives: "not found" means
 * the goal is met.
 */
export async function revokeSession(sid: string): Promise<void> {
  try {
    await requestEmpty(`/v1/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
  } catch (error) {
    if (!isApiError(error, 404)) throw error;
  }
}
