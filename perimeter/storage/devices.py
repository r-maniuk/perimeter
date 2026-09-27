"""Latest known device state (fleet-wide), viewports, and which of a user's zones a device is in.

Viewports use :func:`perimeter.storage.spatial.devices_in_bbox`, which tests positions against the
box as a ``geography`` polygon so that the GiST index on ``devices.position`` does the work. The
edges of such a polygon are geodesics, not lines of constant latitude, which :func:`in_viewport`
corrects for so that the answer is exactly the devices inside the lon/lat rectangle, at any size:

* PostGIS refuses edges of 180 degrees of arc (a box from pole to pole has two), so a viewport is
  split at the antimeridian and into parts at most 90 degrees wide and tall;
* a geodesic between two points of one parallel bends towards the pole, so the edge on the equator
  side of a part cuts into the rectangle (by degrees for wide parts, metres at city scale). Moving
  that edge to latitude ``atan(cos(Δλ/2)·tan φ)`` makes the geodesic's apex touch the original
  parallel, so each query polygon contains its part entirely;
* rows found are then checked against the exact rectangle.
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.storage.models import Device, GeoZone, ZonePresence
from perimeter.storage.spatial import DevicePosition, devices_in_bbox
from perimeter.storage.zones import latitude, longitude, real

MAX_PART_DEGREES = 90.0
EDGE_PADDING_DEGREES = 1e-7  # against rounding exactly on a part's edge (about a centimetre)

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


def _split(low: float, high: float) -> list[tuple[float, float]]:
    pieces = max(1, math.ceil((high - low) / MAX_PART_DEGREES))
    step = (high - low) / pieces
    return [
        (low + i * step, high if i == pieces - 1 else low + (i + 1) * step) for i in range(pieces)
    ]


def viewport_parts(bbox: BBox) -> list[BBox]:
    """``bbox`` as parts at most :data:`MAX_PART_DEGREES` wide and tall, none crossing 180°."""
    west, south, east, north = bbox
    spans = [(west, east)] if west <= east else [(west, 180.0), (-180.0, east)]
    return [
        (part_west, part_south, part_east, part_north)
        for part_south, part_north in _split(south, north)
        for low, high in spans
        for part_west, part_east in _split(low, high)
    ]


def inside(bbox: BBox, lon: float, lat: float) -> bool:
    west, south, east, north = bbox
    if not south <= lat <= north:
        return False
    if west <= east:
        return west <= lon <= east
    return lon >= west or lon <= east


def enclosing_box(part: BBox) -> BBox:
    """A box whose geodesic polygon contains the lon/lat rectangle ``part`` (at most 90° wide)."""
    west, south, east, north = part
    half_width_cos = math.cos(math.radians(east - west) / 2)
    if south > 0:
        south = math.degrees(math.atan(half_width_cos * math.tan(math.radians(south))))
    if north < 0:
        north = math.degrees(math.atan(half_width_cos * math.tan(math.radians(north))))
    pad = EDGE_PADDING_DEGREES
    return (
        max(-180.0, west - pad),
        max(-90.0, south - pad),
        min(180.0, east + pad),
        min(90.0, north + pad),
    )


async def in_viewport(
    conn: AsyncConnection, bbox: BBox, *, stale_s: float, limit: int
) -> tuple[list[DevicePosition], bool]:
    """Live devices inside ``bbox`` ordered by id, at most ``limit``, and whether more matched."""
    found: dict[str, DevicePosition] = {}
    truncated = False
    for part in viewport_parts(bbox):
        west, south, east, north = enclosing_box(part)
        rows = await devices_in_bbox(
            conn, west=west, south=south, east=east, north=north, stale_s=stale_s, limit=limit + 1
        )
        truncated = truncated or len(rows) > limit
        found.update((row.device_id, _clean(row)) for row in rows if inside(bbox, row.lon, row.lat))
    ordered = [found[key] for key in sorted(found)]
    return ordered[:limit], truncated or len(ordered) > limit
