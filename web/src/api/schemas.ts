/**
 * Runtime shapes of everything the API sends: REST resources and live-channel frames.
 *
 * Responses are validated at the boundary so a contract drift surfaces as one precise error
 * ("zones[3].radius_m: expected number") instead of a blank panel three components later. Objects
 * are loose (unknown keys pass through) so the server can add fields without breaking clients.
 */
import * as v from "valibot";

const Id = v.string();
const Timestamp = v.string();
const LatLon = v.looseObject({ lat: v.number(), lon: v.number() });

export const UserSchema = v.looseObject({ id: Id, username: v.string() });
export type User = v.InferOutput<typeof UserSchema>;

/**
 * `POST /v1/session`: a browser sign-in. The token itself is only ever in the `HttpOnly` cookie,
 * never in the body, so no script on the page can read it.
 */
export const SessionSchema = v.looseObject({
  expires_at: Timestamp,
  user: UserSchema,
});
export type SignInResult = v.InferOutput<typeof SessionSchema>;

export const ZoneSchema = v.looseObject({
  id: Id,
  name: v.string(),
  color: v.string(),
  center: LatLon,
  radius_m: v.number(),
  is_active: v.boolean(),
  notify_enter: v.boolean(),
  notify_exit: v.boolean(),
  dwell_s: v.nullable(v.number()),
  version: v.number(),
  created_at: Timestamp,
  updated_at: Timestamp,
  occupancy: v.optional(v.number()),
});
export type Zone = v.InferOutput<typeof ZoneSchema>;

/** Keyset page: `{items, next_cursor}`. */
export function pageOf<T extends v.GenericSchema>(item: T) {
  return v.looseObject({
    items: v.array(item),
    next_cursor: v.nullish(v.string()),
  });
}

export const ZonePageSchema = pageOf(ZoneSchema);

export const AlertKindSchema = v.picklist(["enter", "exit", "dwell"]);
export type AlertKind = v.InferOutput<typeof AlertKindSchema>;

export const AlertSchema = v.looseObject({
  id: Id,
  kind: AlertKindSchema,
  device_id: v.string(),
  /** `id` becomes null once the zone is deleted; the name stays for the history. */
  zone: v.looseObject({ id: v.nullable(Id), name: v.string() }),
  position: LatLon,
  occurred_at: Timestamp,
  created_at: v.optional(Timestamp),
});
export type AlertRecord = v.InferOutput<typeof AlertSchema>;

export const AlertPageSchema = pageOf(AlertSchema);

export const OccupantSchema = v.looseObject({
  device_id: v.string(),
  entered_at: v.optional(v.nullable(Timestamp)),
  last_seen_at: v.optional(v.nullable(Timestamp)),
  recorded_at: v.optional(v.nullable(Timestamp)),
  position: v.optional(v.nullable(LatLon)),
  speed_mps: v.optional(v.nullable(v.number())),
  heading_deg: v.optional(v.nullable(v.number())),
});
export type Occupant = v.InferOutput<typeof OccupantSchema>;

export const OccupantListSchema = v.looseObject({
  /** Devices inside the zone; may exceed the items returned. */
  occupancy: v.number(),
  items: v.array(OccupantSchema),
});

const PointGeometry = v.looseObject({
  type: v.literal("Point"),
  coordinates: v.tuple([v.number(), v.number()]),
});

/** One of the viewer's zones a device is inside (the engine's presence), newest arrival first. */
export const DeviceZoneSchema = v.looseObject({
  id: Id,
  name: v.string(),
  color: v.string(),
  entered_at: Timestamp,
  last_seen_at: Timestamp,
});

/** `GET /v1/devices/{id}`: the device's latest state and the viewer's zones it is in. */
export const DeviceFeatureSchema = v.looseObject({
  type: v.literal("Feature"),
  id: v.optional(v.union([v.string(), v.number()])),
  geometry: PointGeometry,
  properties: v.looseObject({
    device_id: v.optional(v.string()),
    recorded_at: v.optional(v.nullable(Timestamp)),
    received_at: v.optional(v.nullable(Timestamp)),
    speed_mps: v.optional(v.nullable(v.number())),
    heading_deg: v.optional(v.nullable(v.number())),
    accuracy_m: v.optional(v.nullable(v.number())),
    zones: v.array(DeviceZoneSchema),
  }),
});
export type DeviceFeature = v.InferOutput<typeof DeviceFeatureSchema>;

const LineGeometry = v.looseObject({
  type: v.literal("LineString"),
  coordinates: v.array(v.tuple([v.number(), v.number()])),
});

/**
 * A device's recent path: a LineString for two or more reports, a Point for one, no geometry for
 * none. `timestamps` carries the device time of each coordinate.
 */
export const TrailSchema = v.looseObject({
  type: v.literal("Feature"),
  geometry: v.nullable(v.union([LineGeometry, PointGeometry])),
  properties: v.looseObject({
    timestamps: v.array(Timestamp),
    /** Start of the window searched: the requested one, cut to how long tracks are kept. */
    since: Timestamp,
    /** False when the point cap or the retention window shortened the trail. */
    complete: v.boolean(),
  }),
});

/** One live socket of the signed-in user, on any replica (`GET /v1/sessions`, `sessions` frames). */
export const LiveSessionSchema = v.looseObject({
  sid: v.string(),
  /** Browser and operating system, for example "Chrome · macOS". */
  label: v.string(),
  agent: v.nullable(v.string()),
  ip: v.nullable(v.string()),
  /** The api replica holding the socket. */
  replica: v.string(),
  connected_at: Timestamp,
  /** Opened with the viewer's own sign-in (another tab of this browser, typically). */
  current: v.boolean(),
});
export type LiveSession = v.InferOutput<typeof LiveSessionSchema>;

export const SessionListSchema = v.looseObject({ sessions: v.array(LiveSessionSchema) });

export const ProblemSchema = v.looseObject({
  type: v.optional(v.string()),
  title: v.optional(v.string()),
  status: v.optional(v.number()),
  detail: v.optional(v.string()),
  code: v.optional(v.string()),
});
export type Problem = v.InferOutput<typeof ProblemSchema>;

/* ------------------------------------------------------------------ live channel frames */

/** Event envelope stored in the EVENTS stream (§7.2). */
export const AlertEventDataSchema = v.looseObject({
  alert_id: Id,
  kind: AlertKindSchema,
  device_id: v.string(),
  zone: v.looseObject({ id: Id, name: v.string() }),
  occurred_at: Timestamp,
  position: LatLon,
});

export const EventEnvelopeSchema = v.variant("type", [
  v.looseObject({
    id: Id,
    type: v.literal("alert"),
    ts: Timestamp,
    data: AlertEventDataSchema,
  }),
  v.looseObject({
    id: Id,
    type: v.picklist(["zone.created", "zone.updated"]),
    ts: Timestamp,
    data: ZoneSchema,
  }),
  v.looseObject({
    id: Id,
    type: v.literal("zone.deleted"),
    ts: Timestamp,
    data: v.looseObject({ id: Id }),
  }),
]);
export type EventEnvelope = v.InferOutput<typeof EventEnvelopeSchema>;

/** Epoch milliseconds on the server's clock. */
const ServerTime = v.number();

/** Always the first frame of a connection. */
export const HelloFrameSchema = v.looseObject({
  type: v.literal("hello"),
  /** This socket; `sessions` frames list it among the user's others. */
  session_id: v.string(),
  user: UserSchema,
  server_time: ServerTime,
  protocol: v.number(),
  resume: v.looseObject({
    mode: v.picklist(["replay", "reset", "fresh"]),
    /** Every event frame that follows on this socket has `seq` > `after`. */
    after: v.number(),
  }),
  tile_zoom: v.number(),
  /** The api replica holding the socket. */
  replica: v.string(),
});
export type HelloFrame = v.InferOutput<typeof HelloFrameSchema>;

export const EventFrameSchema = v.looseObject({
  type: v.literal("event"),
  seq: v.number(),
  prev: v.number(),
  event: v.unknown(),
});
export type EventFrame = v.InferOutput<typeof EventFrameSchema>;

export const PulseFrameSchema = v.looseObject({
  type: v.literal("pulse"),
  window_ms: v.optional(v.number()),
  zones: v.record(v.string(), v.array(v.string())),
});
export type PulseFrame = v.InferOutput<typeof PulseFrameSchema>;

export const SessionsFrameSchema = v.looseObject({
  type: v.literal("sessions"),
  sessions: v.array(LiveSessionSchema),
});

export const ResyncFrameSchema = v.looseObject({
  type: v.literal("resync"),
  scope: v.optional(v.string()),
});

export const ServiceSnapshotSchema = v.looseObject({
  service: v.string(),
  instance: v.string(),
  ts: v.optional(v.number()),
  loop_lag_p99_ms: v.optional(v.nullable(v.number())),
});
export type ServiceSnapshot = v.InferOutput<typeof ServiceSnapshotSchema> & Record<string, unknown>;

export const OpsFrameSchema = v.looseObject({
  type: v.literal("ops"),
  services: v.array(ServiceSnapshotSchema),
  /** Epoch seconds when the board assembled the frame. */
  ts: v.number(),
});
export type OpsFrame = v.InferOutput<typeof OpsFrameSchema>;

export const PongFrameSchema = v.looseObject({
  type: v.literal("pong"),
  t: v.number(),
  server_time: ServerTime,
});

export const ServerFrameSchema = v.variant("type", [
  HelloFrameSchema,
  EventFrameSchema,
  PulseFrameSchema,
  SessionsFrameSchema,
  ResyncFrameSchema,
  OpsFrameSchema,
  PongFrameSchema,
]);
export type ServerFrame = v.InferOutput<typeof ServerFrameSchema>;
