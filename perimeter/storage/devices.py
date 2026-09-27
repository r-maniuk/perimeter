"""Latest known device state (fleet-wide), viewports, and which of a user's zones a device is in.

Viewports are lon/lat rectangles and :func:`perimeter.storage.spatial.devices_in_bbox` answers them
exactly with a planar index, at any size, including rectangles that cross the antimeridian or span
pole to pole.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.storage.models import Device, GeoZone, ZonePresence
from perimeter.storage.spatial import DevicePosition, devices_in_bbox
from perimeter.storage.zones import latitude, longitude, real

BBox = tuple[float, float, float, float]
"""``(west, south, east, north)`` in degrees; ``west > east`` crosses the antimeridian."""


@dataclass(frozen=True, slots=True)
class DeviceState:
    device_id: str
    lat: float
    lon: float
    recorded_at: datetime
    received_at: datetime
    speed_mps: float | None
    heading_deg: float | None
    accuracy_m: float | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceZone:
    """One of the caller's zones that the device is inside."""

    zone_id: uuid.UUID
    name: str
    color: str
    entered_at: datetime
    last_seen_at: datetime


async def get(conn: AsyncConnection, device_id: str) -> DeviceState | None:
    statement = select(
        Device.device_id,
        latitude(Device.position).label("lat"),
        longitude(Device.position).label("lon"),
        Device.recorded_at,
        Device.received_at,
        Device.speed_mps,
        Device.heading_deg,
        Device.accuracy_m,
        Device.updated_at,
    ).where(Device.device_id == device_id)
    row = (await conn.execute(statement)).one_or_none()
    if row is None:
        return None
    return DeviceState(
        device_id=row.device_id,
        lat=row.lat,
        lon=row.lon,
        recorded_at=row.recorded_at,
        received_at=row.received_at,
        speed_mps=real(row.speed_mps),
        heading_deg=real(row.heading_deg),
        accuracy_m=real(row.accuracy_m),
        updated_at=row.updated_at,
    )


async def zones_inside(
    conn: AsyncConnection, device_id: str, owner_id: uuid.UUID
) -> list[DeviceZone]:
    """The owner's zones that currently contain the device (presence rows), newest stay first."""
    statement = (
        select(
            GeoZone.id,
            GeoZone.name,
            GeoZone.color,
            ZonePresence.entered_at,
            ZonePresence.last_seen_at,
        )
        .join_from(ZonePresence, GeoZone, GeoZone.id == ZonePresence.zone_id)
        .where(ZonePresence.device_id == device_id, GeoZone.owner_id == owner_id)
        .order_by(ZonePresence.entered_at.desc(), GeoZone.id)
    )
    return [DeviceZone(*row) for row in await conn.execute(statement)]


async def live(conn: AsyncConnection, *, stale_s: float, limit: int) -> list[DevicePosition]:
    """Every device that reported within ``stale_s`` seconds, ordered by id."""
    statement = (
        select(
            Device.device_id,
            latitude(Device.position).label("lat"),
            longitude(Device.position).label("lon"),
            Device.recorded_at,
            Device.speed_mps,
            Device.heading_deg,
        )
        .where(Device.recorded_at > func.now() - timedelta(seconds=stale_s))
        .order_by(Device.device_id)
        .limit(limit)
    )
    return [_clean(DevicePosition(*row)) for row in await conn.execute(statement)]


def _clean(position: DevicePosition) -> DevicePosition:
    return dataclasses.replace(
        position, speed_mps=real(position.speed_mps), heading_deg=real(position.heading_deg)
    )


def inside(bbox: BBox, lon: float, lat: float) -> bool:
    west, south, east, north = bbox
    if not south <= lat <= north:
        return False
    if west <= east:
        return west <= lon <= east
    return lon >= west or lon <= east


async def in_viewport(
    conn: AsyncConnection, bbox: BBox, *, stale_s: float, limit: int
) -> tuple[list[DevicePosition], bool]:
    """Live devices inside ``bbox`` ordered by id, at most ``limit``, and whether more matched."""
    west, south, east, north = bbox
    rows = await devices_in_bbox(
        conn, west=west, south=south, east=east, north=north, stale_s=stale_s, limit=limit + 1
    )
    return [_clean(row) for row in rows[:limit]], len(rows) > limit
