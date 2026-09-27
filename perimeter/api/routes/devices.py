"""Latest device positions (GeoJSON), one device's state, and its recent trail.

Positions are fleet-wide: every signed-in user sees every device, while the zones a device is in
are reported only for the caller's own zones. Collections can hold thousands of features, so they
are encoded by msgspec straight from the rows (the pydantic models in :mod:`perimeter.api.schemas`
describe the same shapes for the OpenAPI document) to keep serialisation off the event loop's
critical path.

Viewports are answered by :func:`perimeter.storage.devices.in_viewport`, exactly, at any size.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Any

import msgspec
from fastapi import APIRouter, Path, Query, Response

from perimeter.api.deps import CurrentUser, State
from perimeter.api.errors import ProblemError, not_found
from perimeter.api.schemas import (
    DeviceCollection,
    DeviceDetail,
    DeviceDetailProperties,
    DeviceZone,
    PointGeometry,
    Trail,
)
from perimeter.bus.trail import read_trail
from perimeter.domain.clock import SYSTEM_CLOCK
from perimeter.domain.reports import DEVICE_ID_PATTERN, ms_to_datetime
from perimeter.storage import devices
from perimeter.storage.devices import BBox

router = APIRouter(prefix="/devices", tags=["devices"])

TRAIL_MAX_POINTS = 5_000
TRAIL_TIMEOUT_S = 2.0
COORDINATE_DECIMALS = 7

DeviceIdPath = Annotated[str, Path(max_length=64, pattern=DEVICE_ID_PATTERN)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "No report from this device has been processed yet"}
}


class _Point(msgspec.Struct, kw_only=True):
    type: str = "Point"
    coordinates: tuple[float, float]


class _LineString(msgspec.Struct, kw_only=True):
    type: str = "LineString"
    coordinates: list[tuple[float, float]]


class _Properties(msgspec.Struct):
    device_id: str
    recorded_at: datetime
    speed_mps: float | None
    heading_deg: float | None


class _Feature(msgspec.Struct, kw_only=True):
    type: str = "Feature"
    id: str
    geometry: _Point
    properties: _Properties


class _Collection(msgspec.Struct, kw_only=True):
    type: str = "FeatureCollection"
    features: list[_Feature]
    truncated: bool


class _TrailProperties(msgspec.Struct):
    device_id: str
    since: datetime
    timestamps: list[datetime]
    speeds: list[float | None]
    complete: bool


class _Trail(msgspec.Struct, kw_only=True):
    type: str = "Feature"
    id: str
    geometry: _LineString | _Point | None
    properties: _TrailProperties


_encoder = msgspec.json.Encoder()


def _json(document: msgspec.Struct) -> Response:
    return Response(_encoder.encode(document), media_type="application/json")


def _coordinates(lon: float, lat: float) -> tuple[float, float]:
    return round(lon, COORDINATE_DECIMALS), round(lat, COORDINATE_DECIMALS)


def parse_bbox(raw: str) -> BBox:
    """``west,south,east,north`` in degrees; ``west > east`` means across the antimeridian."""
    parts = raw.split(",")
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError as exc:
        raise _bad_bbox("bbox must be four numbers: west,south,east,north") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise _bad_bbox("bbox values must be finite")
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise _bad_bbox("longitudes must lie within [-180, 180]")
    if not -90 <= south <= north <= 90:
        raise _bad_bbox("latitudes must satisfy -90 <= south <= north <= 90")
    return west, south, east, north


def _bad_bbox(detail: str) -> ProblemError:
    return ProblemError(422, "invalid_bbox", detail)


@router.get(
    "",
    response_model=DeviceCollection,
    summary="Latest positions of live devices (GeoJSON FeatureCollection)",
)
async def list_devices(
    _: CurrentUser,
    state: State,
    bbox: Annotated[
        str | None,
        Query(
            description="`west,south,east,north` in degrees (omit for everywhere)",
            examples=["4.78,52.32,5.02,52.42"],
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1_000,
) -> Response:
    box = parse_bbox(bbox) if bbox is not None else None
    stale_s = state.settings.live.device_stale_s
    async with state.db.connect() as conn:
        if box is None:
            rows = await devices.live(conn, stale_s=stale_s, limit=limit + 1)
            positions, truncated = rows[:limit], len(rows) > limit
        else:
            positions, truncated = await devices.in_viewport(
                conn, box, stale_s=stale_s, limit=limit
            )
    features = [
        _Feature(
            id=p.device_id,
            geometry=_Point(coordinates=_coordinates(p.lon, p.lat)),
            properties=_Properties(p.device_id, p.recorded_at, p.speed_mps, p.heading_deg),
        )
        for p in positions
    ]
    return _json(_Collection(features=features, truncated=truncated))


@router.get(
    "/{device_id}",
    response_model=DeviceDetail,
    responses=_NOT_FOUND,
    summary="Latest state of one device, with the zones of yours it is in",
)
async def get_device(device_id: DeviceIdPath, principal: CurrentUser, state: State) -> DeviceDetail:
    async with state.db.connect() as conn:
        device = await devices.get(conn, device_id)
        if device is None:
            raise not_found("device")
        zones = await devices.zones_inside(conn, device_id, principal.user_id)
    return DeviceDetail(
        id=device.device_id,
        geometry=PointGeometry(coordinates=_coordinates(device.lon, device.lat)),
        properties=DeviceDetailProperties(
            device_id=device.device_id,
            recorded_at=device.recorded_at,
            received_at=device.received_at,
            speed_mps=device.speed_mps,
            heading_deg=device.heading_deg,
            accuracy_m=device.accuracy_m,
            updated_at=device.updated_at,
            zones=[
                DeviceZone(
                    id=zone.zone_id,
                    name=zone.name,
                    color=zone.color,
                    entered_at=zone.entered_at,
                    last_seen_at=zone.last_seen_at,
                )
                for zone in zones
            ],
        ),
    )


@router.get(
    "/{device_id}/trail",
    response_model=Trail,
    responses=_NOT_FOUND,
    summary="Recent track of one device from the telemetry log (GeoJSON Feature)",
)
async def device_trail(
    device_id: DeviceIdPath,
    _: CurrentUser,
    state: State,
    minutes: Annotated[int, Query(ge=1, le=120, description="How far back to go")] = 15,
) -> Response:
    async with state.db.connect() as conn:
        if await devices.get(conn, device_id) is None:
            raise not_found("device")
    now_ms = SYSTEM_CLOCK.now_ms()
    trail = await read_trail(
        state.nc,
        device_id,
        since_ms=now_ms - minutes * 60_000,
        now_ms=now_ms,
        max_points=TRAIL_MAX_POINTS,
        timeout_s=TRAIL_TIMEOUT_S,
    )
    coordinates = [_coordinates(p.lon, p.lat) for p in trail.points]
    geometry: _LineString | _Point | None = None
    if len(coordinates) >= 2:
        geometry = _LineString(coordinates=coordinates)
    elif coordinates:
        geometry = _Point(coordinates=coordinates[0])
    return _json(
        _Trail(
            id=device_id,
            geometry=geometry,
            properties=_TrailProperties(
                device_id=device_id,
                since=ms_to_datetime(trail.since_ms),
                timestamps=[ms_to_datetime(p.recorded_at_ms) for p in trail.points],
                speeds=[p.speed for p in trail.points],
                complete=trail.complete,
            ),
        )
    )
