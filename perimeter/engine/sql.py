"""Every statement on the engine's hot path.

All of them are set-based: a batch of any size costs the same number of round trips, because the
batch travels as arrays that ``unnest`` expands on the server. All of them are index-driven: reads
go through primary keys (``devices``, ``zone_presence``, ``geozones``), the spatial match through
the zones' envelope GiST index (:func:`perimeter.storage.spatial.match_zones`), and writes touch
only the rows of the batch's own devices.

Timestamps cross the wire as integer epoch milliseconds and PostgreSQL converts them both ways.
``to_timestamp(ms / 1000.0)`` rounds to whole microseconds, which recovers the exact millisecond,
and ``extract(epoch ...)`` returns ``numeric``, so reading them back is exact too: "is this report
newer than the last committed one" is decided on the integers the device sent, never on a float
that lost its last millisecond on the way.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, BOOLEAN, DOUBLE_PRECISION, REAL, TEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.bus.leases import FencingToken
from perimeter.domain.presence import Stay, Transition, ZoneRules
from perimeter.domain.reports import TelemetryRecord

_TEXTS = ARRAY(TEXT)
_UUIDS = ARRAY(PG_UUID(as_uuid=True))
_FLOATS = ARRAY(DOUBLE_PRECISION)
_REALS = ARRAY(REAL)
_BIGINTS = ARRAY(BIGINT)
_BOOLS = ARRAY(BOOLEAN)

# The fencing token of the batch must be at least the newest one this partition has seen. The row
# lock taken here also serialises the batches of one partition: a zombie and its successor can
# never interleave their reads and writes of the same devices.
FENCE = text(
    """
    INSERT INTO partition_epochs AS e (partition, generation, revision, owner, updated_at)
    VALUES (:partition, :generation, :revision, :owner, now())
    ON CONFLICT (partition) DO UPDATE
       SET generation = EXCLUDED.generation,
           revision = EXCLUDED.revision,
           owner = EXCLUDED.owner,
           updated_at = EXCLUDED.updated_at
     WHERE (e.generation, e.revision) <= (EXCLUDED.generation, EXCLUDED.revision)
    RETURNING e.revision
    """
)

LAST_RECORDED = text(
    """
    SELECT device_id, (extract(epoch FROM recorded_at) * 1000)::bigint AS recorded_ms
    FROM devices
    WHERE device_id = ANY(CAST(:device_ids AS text[]))
    """
).bindparams(bindparam("device_ids", type_=_TEXTS))

PRESENCE = text(
    """
    SELECT device_id, zone_id,
           (extract(epoch FROM entered_at) * 1000)::bigint AS entered_ms,
           (extract(epoch FROM last_seen_at) * 1000)::bigint AS last_seen_ms,
           dwell_alerted
    FROM zone_presence
    WHERE device_id = ANY(CAST(:device_ids AS text[]))
    """
).bindparams(bindparam("device_ids", type_=_TEXTS))

# FOR KEY SHARE holds off deleting or deactivating these zones until the batch commits (the API
# takes a full row lock for those; renames and moves go ahead), so every presence row and alert the
# batch writes still has its zone, active, at commit time. Checking existence with a subquery
# instead would only narrow that race: the foreign key is checked against the latest committed
# state, not the statement's snapshot.
ZONE_RULES = text(
    """
    SELECT id, owner_id, name, is_active, notify_enter, notify_exit, dwell_s
    FROM geozones
    WHERE id = ANY(CAST(:zone_ids AS uuid[]))
    ORDER BY id
    FOR KEY SHARE
    """
).bindparams(bindparam("zone_ids", type_=_UUIDS))

UPSERT_DEVICES = text(
    """
    INSERT INTO devices AS d (device_id, position, recorded_at, received_at,
                              speed_mps, heading_deg, accuracy_m, updated_at)
    SELECT r.device_id,
           ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)::geography,
           to_timestamp(r.recorded_ms / 1000.0),
           to_timestamp(r.received_ms / 1000.0),
           r.speed, r.heading, r.accuracy, now()
    FROM unnest(CAST(:device_ids AS text[]), CAST(:lons AS float8[]), CAST(:lats AS float8[]),
                CAST(:recorded AS bigint[]), CAST(:received AS bigint[]),
                CAST(:speeds AS real[]), CAST(:headings AS real[]), CAST(:accuracies AS real[]))
         AS r(device_id, lon, lat, recorded_ms, received_ms, speed, heading, accuracy)
    ON CONFLICT (device_id) DO UPDATE
       SET position = EXCLUDED.position,
           recorded_at = EXCLUDED.recorded_at,
           received_at = EXCLUDED.received_at,
           speed_mps = EXCLUDED.speed_mps,
           heading_deg = EXCLUDED.heading_deg,
           accuracy_m = EXCLUDED.accuracy_m,
           updated_at = EXCLUDED.updated_at
     WHERE d.recorded_at < EXCLUDED.recorded_at
    RETURNING d.device_id
    """
).bindparams(
    bindparam("device_ids", type_=_TEXTS),
    bindparam("lons", type_=_FLOATS),
    bindparam("lats", type_=_FLOATS),
    bindparam("recorded", type_=_BIGINTS),
    bindparam("received", type_=_BIGINTS),
    bindparam("speeds", type_=_REALS),
    bindparam("headings", type_=_REALS),
    bindparam("accuracies", type_=_REALS),
)

UPSERT_PRESENCE = text(
    """
    INSERT INTO zone_presence AS p (device_id, zone_id, entered_at, last_seen_at, dwell_alerted)
    SELECT s.device_id, s.zone_id,
           to_timestamp(s.entered_ms / 1000.0), to_timestamp(s.last_seen_ms / 1000.0), s.alerted
    FROM unnest(CAST(:device_ids AS text[]), CAST(:zone_ids AS uuid[]),
                CAST(:entered AS bigint[]), CAST(:last_seen AS bigint[]),
                CAST(:alerted AS boolean[]))
         AS s(device_id, zone_id, entered_ms, last_seen_ms, alerted)
    ON CONFLICT (device_id, zone_id) DO UPDATE
       SET entered_at = EXCLUDED.entered_at,
           last_seen_at = EXCLUDED.last_seen_at,
           dwell_alerted = EXCLUDED.dwell_alerted
    """
).bindparams(
    bindparam("device_ids", type_=_TEXTS),
    bindparam("zone_ids", type_=_UUIDS),
    bindparam("entered", type_=_BIGINTS),
    bindparam("last_seen", type_=_BIGINTS),
    bindparam("alerted", type_=_BOOLS),
)

DELETE_PRESENCE = text(
    """
    DELETE FROM zone_presence AS p
    USING unnest(CAST(:device_ids AS text[]), CAST(:zone_ids AS uuid[])) AS x(device_id, zone_id)
    WHERE p.device_id = x.device_id AND p.zone_id = x.zone_id
    """
).bindparams(bindparam("device_ids", type_=_TEXTS), bindparam("zone_ids", type_=_UUIDS))

# Every applied report of the batch (not only each device's newest), for trails. The primary key
# makes a replay harmless even if a report were ever applied twice.
INSERT_TRACKS = text(
    """
    INSERT INTO device_tracks (device_id, recorded_at, position, speed_mps, heading_deg)
    SELECT r.device_id, to_timestamp(r.recorded_ms / 1000.0),
           ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)::geography, r.speed, r.heading
    FROM unnest(CAST(:device_ids AS text[]), CAST(:recorded AS bigint[]),
                CAST(:lons AS float8[]), CAST(:lats AS float8[]),
                CAST(:speeds AS real[]), CAST(:headings AS real[]))
         AS r(device_id, recorded_ms, lon, lat, speed, heading)
    ON CONFLICT DO NOTHING
    """
).bindparams(
    bindparam("device_ids", type_=_TEXTS),
    bindparam("recorded", type_=_BIGINTS),
    bindparam("lons", type_=_FLOATS),
    bindparam("lats", type_=_FLOATS),
    bindparam("speeds", type_=_REALS),
    bindparam("headings", type_=_REALS),
)

# Redelivery never reaches this statement with an already raised alert (late reports produce
# none), so the conflict clause is a last line of defence, not the de-duplication mechanism.
INSERT_ALERTS = text(
    """
    INSERT INTO alerts (id, owner_id, zone_id, zone_name, device_id, kind, position, occurred_at)
    SELECT a.id, a.owner_id, a.zone_id, a.zone_name, a.device_id, a.kind,
           ST_SetSRID(ST_MakePoint(a.lon, a.lat), 4326)::geography,
           to_timestamp(a.occurred_ms / 1000.0)
    FROM unnest(CAST(:ids AS uuid[]), CAST(:owner_ids AS uuid[]), CAST(:zone_ids AS uuid[]),
                CAST(:zone_names AS text[]), CAST(:device_ids AS text[]), CAST(:kinds AS text[]),
                CAST(:lons AS float8[]), CAST(:lats AS float8[]), CAST(:occurred AS bigint[]))
         AS a(id, owner_id, zone_id, zone_name, device_id, kind, lon, lat, occurred_ms)
    ON CONFLICT DO NOTHING
    RETURNING id
    """
).bindparams(
    bindparam("ids", type_=_UUIDS),
    bindparam("owner_ids", type_=_UUIDS),
    bindparam("zone_ids", type_=_UUIDS),
    bindparam("zone_names", type_=_TEXTS),
    bindparam("device_ids", type_=_TEXTS),
    bindparam("kinds", type_=_TEXTS),
    bindparam("lons", type_=_FLOATS),
    bindparam("lats", type_=_FLOATS),
    bindparam("occurred", type_=_BIGINTS),
)


async def fence(conn: AsyncConnection, *, partition: int, token: FencingToken, owner: str) -> bool:
    """Claim the partition for this transaction; ``False`` if a newer token has been seen."""
    result = await conn.execute(
        FENCE,
        {
            "partition": partition,
            "generation": token.generation,
            "revision": token.revision,
            "owner": owner,
        },
    )
    return result.first() is not None


async def last_recorded(conn: AsyncConnection, device_ids: Sequence[str]) -> dict[str, int]:
    """Event time (epoch ms) of the newest committed report of each known device."""
    if not device_ids:
        return {}
    result = await conn.execute(LAST_RECORDED, {"device_ids": list(device_ids)})
    return dict(result.all())


async def presence(conn: AsyncConnection, device_ids: Sequence[str]) -> dict[str, dict[UUID, Stay]]:
    """The zones each device is currently inside."""
    stays: dict[str, dict[UUID, Stay]] = {}
    if not device_ids:
        return stays
    result = await conn.execute(PRESENCE, {"device_ids": list(device_ids)})
    for device_id, zone_id, entered_ms, last_seen_ms, alerted in result:
        stays.setdefault(device_id, {})[zone_id] = Stay(
            zone_id, entered_at_ms=entered_ms, last_seen_at_ms=last_seen_ms, dwell_alerted=alerted
        )
    return stays


async def zone_rules(conn: AsyncConnection, zone_ids: Collection[UUID]) -> dict[UUID, ZoneRules]:
    """Rules of the zones that still exist, locked against deletion until commit."""
    if not zone_ids:
        return {}
    result = await conn.execute(ZONE_RULES, {"zone_ids": sorted(zone_ids)})
    return {
        zone_id: ZoneRules(
            zone_id=zone_id,
            owner_id=owner_id,
            name=name,
            is_active=is_active,
            notify_enter=notify_enter,
            notify_exit=notify_exit,
            dwell_s=dwell_s,
        )
        for zone_id, owner_id, name, is_active, notify_enter, notify_exit, dwell_s in result
    }


async def upsert_devices(conn: AsyncConnection, records: Sequence[TelemetryRecord]) -> set[str]:
    """Store each device's newest position; returns the devices whose row actually moved on.

    ``records`` holds at most one record per device (the newest of the batch).
    """
    if not records:
        return set()
    result = await conn.execute(
        UPSERT_DEVICES,
        {
            "device_ids": [r.device_id for r in records],
            "lons": [r.lon for r in records],
            "lats": [r.lat for r in records],
            "recorded": [r.recorded_at_ms for r in records],
            "received": [r.received_at_ms for r in records],
            "speeds": [r.speed for r in records],
            "headings": [r.heading for r in records],
            "accuracies": [r.accuracy for r in records],
        },
    )
    return set(result.scalars())


async def insert_tracks(conn: AsyncConnection, records: Sequence[TelemetryRecord]) -> None:
    if not records:
        return
    await conn.execute(
        INSERT_TRACKS,
        {
            "device_ids": [r.device_id for r in records],
            "recorded": [r.recorded_at_ms for r in records],
            "lons": [r.lon for r in records],
            "lats": [r.lat for r in records],
            "speeds": [r.speed for r in records],
            "headings": [r.heading for r in records],
        },
    )


async def upsert_presence(conn: AsyncConnection, stays: Sequence[tuple[str, Stay]]) -> None:
    if not stays:
        return
    await conn.execute(
        UPSERT_PRESENCE,
        {
            "device_ids": [device_id for device_id, _ in stays],
            "zone_ids": [stay.zone_id for _, stay in stays],
            "entered": [stay.entered_at_ms for _, stay in stays],
            "last_seen": [stay.last_seen_at_ms for _, stay in stays],
            "alerted": [stay.dwell_alerted for _, stay in stays],
        },
    )


async def delete_presence(conn: AsyncConnection, pairs: Sequence[tuple[str, UUID]]) -> None:
    if not pairs:
        return
    await conn.execute(
        DELETE_PRESENCE,
        {
            "device_ids": [device_id for device_id, _ in pairs],
            "zone_ids": [zone_id for _, zone_id in pairs],
        },
    )


async def insert_alerts(
    conn: AsyncConnection, alerts: Sequence[tuple[UUID, Transition]]
) -> set[UUID]:
    """Insert alerts under the given ids; returns the ids that were actually inserted."""
    if not alerts:
        return set()
    result = await conn.execute(
        INSERT_ALERTS,
        {
            "ids": [alert_id for alert_id, _ in alerts],
            "owner_ids": [t.zone.owner_id for _, t in alerts],
            "zone_ids": [t.zone.zone_id for _, t in alerts],
            "zone_names": [t.zone.name for _, t in alerts],
            "device_ids": [t.device_id for _, t in alerts],
            "kinds": [t.kind.value for _, t in alerts],
            "lons": [t.lon for _, t in alerts],
            "lats": [t.lat for _, t in alerts],
            "occurred": [t.occurred_at_ms for _, t in alerts],
        },
    )
    return set(result.scalars())
