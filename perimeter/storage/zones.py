"""Geozones: owner-scoped reads and writes, occupancy and occupants.

Every statement filters by ``owner_id``, so another user's zone is indistinguishable from a zone
that does not exist: the API answers 404 for both and never confirms that a foreign id is real.

*Occupancy* is the number of devices inside a zone right now, i.e. its ``zone_presence`` rows (the
engine maintains them). It is computed inside the same statement that reads the zones, as a
correlated count per returned zone — one index probe on ``zone_presence (zone_id)`` each, one round
trip per page, no N+1.

Edits lock the zone row with ``FOR NO KEY UPDATE``, which conflicts with other edits of the same
zone but not with the ``FOR KEY SHARE`` locks the engine's foreign-key checks take when it writes
presence rows and alerts: editing a zone never stalls geofence processing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    delete,
    func,
    insert,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.storage.models import Device, GeoZone, ZonePresence

WGS84 = 4326
REAL_DECIMALS = 2


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    """Everything a client decides about a zone; the rest is maintained by the database."""

    name: str
    color: str
    lat: float
    lon: float
    radius_m: float
    is_active: bool = True
    notify_enter: bool = True
    notify_exit: bool = True
    dwell_s: int | None = None


@dataclass(frozen=True, slots=True)
class ZoneRecord:
    id: uuid.UUID
    owner_id: uuid.UUID
    spec: ZoneSpec
    version: int
    created_at: datetime
    updated_at: datetime
    occupancy: int


@dataclass(frozen=True, slots=True)
class OccupantRecord:
    device_id: str
    lat: float
    lon: float
    recorded_at: datetime
    speed_mps: float | None
    heading_deg: float | None
    entered_at: datetime
    last_seen_at: datetime


def point(lon: float, lat: float) -> ColumnElement[Any]:
    """A WGS84 ``geography`` point (longitude first, as PostGIS expects)."""
    return func.geography(func.ST_SetSRID(func.ST_MakePoint(lon, lat), WGS84))


def latitude(column: Any) -> ColumnElement[float]:
    return func.ST_Y(func.geometry(column))


def longitude(column: Any) -> ColumnElement[float]:
    return func.ST_X(func.geometry(column))


def real(value: float | None) -> float | None:
    """A ``real`` (float4) column without its binary noise: 8.100000381469727 becomes 8.1.

    Speeds, headings and accuracies are stored as float4; two decimals (cm/s, centidegrees,
    centimetres) is everything they carry.
    """
    return round(value, REAL_DECIMALS) if value is not None else None


_COLUMNS = (
    GeoZone.id,
    GeoZone.owner_id,
    GeoZone.name,
    GeoZone.color,
    latitude(GeoZone.center).label("lat"),
    longitude(GeoZone.center).label("lon"),
    GeoZone.radius_m,
    GeoZone.is_active,
    GeoZone.notify_enter,
    GeoZone.notify_exit,
    GeoZone.dwell_s,
    GeoZone.version,
    GeoZone.created_at,
    GeoZone.updated_at,
)

_OCCUPANCY = (
    select(func.count())
    .where(ZonePresence.zone_id == GeoZone.id)
    .correlate(GeoZone)
    .scalar_subquery()
    .label("occupancy")
)


def _record(row: Row[*tuple[Any, ...]]) -> ZoneRecord:
    return ZoneRecord(
        id=row.id,
        owner_id=row.owner_id,
        spec=ZoneSpec(
            name=row.name,
            color=row.color,
            lat=row.lat,
            lon=row.lon,
            radius_m=row.radius_m,
            is_active=row.is_active,
            notify_enter=row.notify_enter,
            notify_exit=row.notify_exit,
            dwell_s=row.dwell_s,
        ),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        occupancy=row.occupancy,
    )


def _values(spec: ZoneSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "color": spec.color,
        "center": point(spec.lon, spec.lat),
        "radius_m": spec.radius_m,
        "is_active": spec.is_active,
        "notify_enter": spec.notify_enter,
        "notify_exit": spec.notify_exit,
        "dwell_s": spec.dwell_s,
    }


async def create(conn: AsyncConnection, owner_id: uuid.UUID, spec: ZoneSpec) -> ZoneRecord:
    statement = (
        insert(GeoZone)
        .values(owner_id=owner_id, **_values(spec))
        .returning(*_COLUMNS, literal(0).label("occupancy"))
    )
    return _record((await conn.execute(statement)).one())


async def get(
    conn: AsyncConnection, owner_id: uuid.UUID, zone_id: uuid.UUID, *, for_update: bool = False
) -> ZoneRecord | None:
    """The zone, or ``None`` when it does not exist or belongs to someone else.

    With ``for_update`` the row stays locked against other edits until the transaction ends.
    """
    statement = select(*_COLUMNS, _OCCUPANCY).where(
        GeoZone.id == zone_id, GeoZone.owner_id == owner_id
    )
    if for_update:
        statement = statement.with_for_update(key_share=True, of=GeoZone)
    row = (await conn.execute(statement)).one_or_none()
    return _record(row) if row is not None else None


def page_query(
    owner_id: uuid.UUID, *, limit: int, after: tuple[datetime, uuid.UUID] | None = None
) -> Select[*tuple[Any, ...]]:
    """Newest first; ``after`` continues behind the ``(created_at, id)`` of the last zone seen."""
    statement = select(*_COLUMNS, _OCCUPANCY).where(GeoZone.owner_id == owner_id)
    if after is not None:
        statement = statement.where(tuple_(GeoZone.created_at, GeoZone.id) < tuple_(*after))
    return statement.order_by(GeoZone.created_at.desc(), GeoZone.id.desc()).limit(limit)


async def page(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[ZoneRecord]:
    statement = page_query(owner_id, limit=limit, after=after)
    return [_record(row) for row in await conn.execute(statement)]


async def replace(conn: AsyncConnection, zone_id: uuid.UUID, spec: ZoneSpec) -> ZoneRecord:
    """Store a new definition and bump the version; the caller holds the row lock."""
    statement = (
        update(GeoZone)
        .where(GeoZone.id == zone_id)
        .values(**_values(spec), version=GeoZone.version + 1, updated_at=func.now())
        .returning(*_COLUMNS, _OCCUPANCY)
    )
    return _record((await conn.execute(statement)).one())


async def clear_presence(conn: AsyncConnection, zone_id: uuid.UUID) -> int:
    """Forget which devices are inside the zone (it was deactivated); returns how many were."""
    result = await conn.execute(delete(ZonePresence).where(ZonePresence.zone_id == zone_id))
    return result.rowcount


async def remove(conn: AsyncConnection, zone_id: uuid.UUID) -> None:
    """Delete a zone; presence rows go with it and its alerts keep their history (zone_id NULL)."""
    await conn.execute(delete(GeoZone).where(GeoZone.id == zone_id))


async def occupants(
    conn: AsyncConnection, zone_id: uuid.UUID, *, limit: int
) -> list[OccupantRecord]:
    """Devices inside the zone with their latest position, most recent arrivals first."""
    statement = (
        select(
            ZonePresence.device_id,
            latitude(Device.position).label("lat"),
            longitude(Device.position).label("lon"),
            Device.recorded_at,
            Device.speed_mps,
            Device.heading_deg,
            ZonePresence.entered_at,
            ZonePresence.last_seen_at,
        )
        .join_from(ZonePresence, Device, Device.device_id == ZonePresence.device_id)
        .where(ZonePresence.zone_id == zone_id)
        .order_by(ZonePresence.entered_at.desc(), ZonePresence.device_id)
        .limit(limit)
    )
    return [
        OccupantRecord(
            device_id=row.device_id,
            lat=row.lat,
            lon=row.lon,
            recorded_at=row.recorded_at,
            speed_mps=real(row.speed_mps),
            heading_deg=real(row.heading_deg),
            entered_at=row.entered_at,
            last_seen_at=row.last_seen_at,
        )
        for row in await conn.execute(statement)
    ]
