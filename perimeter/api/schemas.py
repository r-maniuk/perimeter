"""Request and response bodies of the REST API: validation, serialisation and OpenAPI docs.

Ingest does not use these models at runtime — reports are decoded by msgspec on the hot path
(:mod:`perimeter.api.ingest.decoding`); the ingest models here document the wire format only.
Large read responses (device collections, trails) are likewise encoded directly and described
here for the OpenAPI document.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from perimeter.domain.presence import TransitionKind

DEFAULT_COLOR = "#6d5dfc"
USERNAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{1,31}$"
COLOR_PATTERN = r"^#[0-9a-f]{6}$"


def _stripped_lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _stripped(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _printable(value: str) -> str:
    if any(unicodedata.category(char) == "Cc" for char in value):
        msg = "must not contain control characters"
        raise ValueError(msg)
    return value


Username = Annotated[
    str,
    BeforeValidator(_stripped_lower),
    StringConstraints(pattern=USERNAME_PATTERN),
    Field(
        description=(
            "2-32 characters: letters, digits, '_', '.', '-', starting with a letter or digit. "
            "Case-insensitive; stored in lower case."
        ),
        examples=["alice"],
    ),
]
Color = Annotated[
    str,
    BeforeValidator(_stripped_lower),
    StringConstraints(pattern=COLOR_PATTERN),
    Field(description="`#rrggbb`; upper-case input is normalised", examples=["#6d5dfc"]),
]
ZoneName = Annotated[
    str,
    BeforeValidator(_stripped),
    StringConstraints(min_length=1, max_length=80),
    AfterValidator(_printable),
    Field(examples=["Dam Square"]),
]
Radius = Annotated[float, Field(ge=10, le=100_000, description="Metres, on the WGS84 spheroid")]
Dwell = Annotated[
    int,
    Field(ge=10, le=86_400, description="Seconds inside the zone before a `dwell` alert"),
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- identity -------------------------------------------------------------------------------


class SessionCreate(_Request):
    model_config = ConfigDict(json_schema_extra={"examples": [{"username": "alice"}]})

    username: Username


class User(BaseModel):
    id: uuid.UUID
    username: str


class Session(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "token_type": "bearer",
                    "expires_at": "2026-09-27T07:07:06Z",
                    "user": {"id": "01925f7e-8a1c-7c3e-9d2a-3b4c5d6e7f80", "username": "alice"},
                }
            ]
        }
    )

    token: str = Field(description="Bearer token for non-browser clients (also set as a cookie)")
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the token scheme, not a secret
    expires_at: datetime
    user: User


class Me(User):
    created_at: datetime
    session_expires_at: datetime


# --- geozones -------------------------------------------------------------------------------


class LatLon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90, examples=[52.3731])
    lon: float = Field(ge=-180, le=180, examples=[4.8926])


_ZONE_EXAMPLE: dict[str, Any] = {
    "name": "Dam Square",
    "center": {"lat": 52.3731, "lon": 4.8926},
    "radius_m": 250,
    "color": "#6d5dfc",
    "notify_enter": True,
    "notify_exit": True,
    "dwell_s": 300,
}


class ZoneCreate(_Request):
    model_config = ConfigDict(json_schema_extra={"examples": [_ZONE_EXAMPLE]})

    name: ZoneName
    center: LatLon
    radius_m: Radius
    color: Color = DEFAULT_COLOR
    is_active: bool = True
    notify_enter: bool = True
    notify_exit: bool = True
    dwell_s: Dwell | None = None


class ZonePatch(_Request):
    """Partial update: absent fields keep their value; only ``dwell_s`` may be set to null."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"radius_m": 400}, {"is_active": False, "dwell_s": None}]}
    )

    name: ZoneName | None = None
    center: LatLon | None = None
    radius_m: Radius | None = None
    color: Color | None = None
    is_active: bool | None = None
    notify_enter: bool | None = None
    notify_exit: bool | None = None
    dwell_s: Dwell | None = None

    @field_validator(
        "name",
        "center",
        "radius_m",
        "color",
        "is_active",
        "notify_enter",
        "notify_exit",
        mode="before",
    )
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            msg = "may be omitted, but not null"
            raise ValueError(msg)
        return value


class Zone(BaseModel):
    """A zone as the API and zone events represent it."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "01925f7e-8a1c-7c3e-9d2a-3b4c5d6e7f81",
                    **_ZONE_EXAMPLE,
                    "is_active": True,
                    "version": 3,
                    "created_at": "2026-09-26T19:00:00Z",
                    "updated_at": "2026-09-26T19:07:06Z",
                    "occupancy": 12,
                }
            ]
        }
    )

    id: uuid.UUID
    name: str
    color: str
    center: LatLon
    radius_m: float
    is_active: bool
    notify_enter: bool
    notify_exit: bool
    dwell_s: int | None
    version: int = Field(description='Incremented by every change; the ETag is `"v<version>"`')
    created_at: datetime
    updated_at: datetime
    occupancy: int = Field(description="Devices inside the zone right now")


class ZonePage(BaseModel):
    items: list[Zone]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null at end")


class Occupant(BaseModel):
    device_id: str
    position: LatLon
    recorded_at: datetime
    speed_mps: float | None
    heading_deg: float | None
    entered_at: datetime
    last_seen_at: datetime


class Occupants(BaseModel):
    zone_id: uuid.UUID
    occupancy: int = Field(description="Devices inside the zone (may exceed the items returned)")
    items: list[Occupant]


# --- alerts ---------------------------------------------------------------------------------


class AlertZone(BaseModel):
    id: uuid.UUID | None = Field(description="null once the zone has been deleted")
    name: str


class Alert(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "01925f7e-9b2d-7a4f-8e3b-4c5d6e7f8091",
                    "kind": "enter",
                    "device_id": "veh-00042",
                    "zone": {"id": "01925f7e-8a1c-7c3e-9d2a-3b4c5d6e7f81", "name": "Dam Square"},
                    "position": {"lat": 52.3729, "lon": 4.8931},
                    "occurred_at": "2026-09-26T19:07:06.131Z",
                    "created_at": "2026-09-26T19:07:06.162Z",
                }
            ]
        }
    )

    id: uuid.UUID
    kind: TransitionKind
    device_id: str
    zone: AlertZone
    position: LatLon
    occurred_at: datetime = Field(description="Device time of the report that caused the alert")
    created_at: datetime


class AlertPage(BaseModel):
    items: list[Alert]
    next_cursor: str | None


# --- devices (GeoJSON, RFC 7946) --------------------------------------------------------------


class PointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float] = Field(description="[longitude, latitude]")


class LineStringGeometry(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]]


class DeviceProperties(BaseModel):
    device_id: str
    recorded_at: datetime
    speed_mps: float | None
    heading_deg: float | None


class DeviceFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: PointGeometry
    properties: DeviceProperties


class DeviceCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[DeviceFeature]
    truncated: bool = Field(description="More devices matched than `limit`")


class DeviceZone(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    entered_at: datetime
    last_seen_at: datetime


class DeviceDetailProperties(DeviceProperties):
    received_at: datetime
    accuracy_m: float | None
    updated_at: datetime
    zones: list[DeviceZone] = Field(description="Your zones the device is inside right now")


class DeviceDetail(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: PointGeometry
    properties: DeviceDetailProperties


class TrailProperties(BaseModel):
    device_id: str
    since: datetime
    timestamps: list[datetime] = Field(description="Device time of each coordinate, in order")
    speeds: list[float | None]
    complete: bool = Field(
        description="false when the point cap or the retention window shortened the trail"
    )


class Trail(BaseModel):
    """A LineString for two or more points, a Point for one, a null geometry for none."""

    type: Literal["Feature"] = "Feature"
    id: str
    geometry: LineStringGeometry | PointGeometry | None
    properties: TrailProperties


# --- ingest (documentation of the msgspec wire format) ----------------------------------------


class IngestRejection(BaseModel):
    index: int = Field(description="Position of the report in the batch")
    code: str = Field(examples=["timestamp_in_future"])
    detail: str


class IngestResult(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "accepted": 249,
                    "rejected": [
                        {
                            "index": 17,
                            "code": "invalid_latitude",
                            "detail": "Expected `float` <= 90.0 - at `$.latitude`",
                        }
                    ],
                }
            ]
        }
    )

    accepted: int = Field(description="Reports durably stored (acknowledged by JetStream)")
    rejected: list[IngestRejection]
