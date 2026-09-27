"""Location reports: the device-facing input schema and the compact record carried on the bus.

Both are ``msgspec`` structs: decoding and validating JSON or MessagePack happens in C, which keeps
the ingestion hot path from spending event-loop time on per-field Python validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import msgspec

# A device id ends up in a broker subject, where a newline is not allowed. Its patterns are
# published in the OpenAPI document, and JavaScript clients (the API reference, generated clients)
# check input with them, so each must mean there what it means to the validator that enforces it;
# JavaScript reads Python's ``\z`` as a plain "z". Path and query parameters are checked by
# pydantic, whose Rust engine takes ``$`` as the end of the text, as JavaScript does. Reports are
# checked by msgspec with Python's ``re``, where ``$`` also matches before a final newline; the
# lookahead refuses that. (Rust has no lookahead, so one pattern cannot serve both.)
DEVICE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
EPOCH_MS_THRESHOLD = 100_000_000_000  # below: seconds, above: milliseconds

DeviceId = Annotated[
    str, msgspec.Meta(min_length=1, max_length=64, pattern=DEVICE_ID_PATTERN + r"(?!\n)")
]
Latitude = Annotated[float, msgspec.Meta(ge=-90.0, le=90.0)]
Longitude = Annotated[float, msgspec.Meta(ge=-180.0, le=180.0)]
Speed = Annotated[float, msgspec.Meta(ge=0.0, le=1_000.0)]
Heading = Annotated[float, msgspec.Meta(ge=0.0, le=360.0)]
Accuracy = Annotated[float, msgspec.Meta(ge=0.0, le=100_000.0)]


class LocationReport(msgspec.Struct, frozen=True, kw_only=True):
    """One position as a device sends it.

    ``timestamp`` is the device clock: an RFC 3339 string (naive values are taken as UTC) or a
    Unix epoch number in seconds or milliseconds. It is required: with the device id it identifies
    the report (a retried report is stored once) and places it in the device's history, which a
    receive time could do for neither.
    """

    device_id: DeviceId
    latitude: Latitude
    longitude: Longitude
    timestamp: datetime | float
    speed: Speed | None = None
    heading: Heading | None = None
    accuracy: Accuracy | None = None

    def recorded_at_ms(self) -> int:
        stamp = self.timestamp
        if isinstance(stamp, datetime):
            aware = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
            return int(aware.timestamp() * 1000)
        if stamp >= EPOCH_MS_THRESHOLD:
            return int(stamp)
        return int(stamp * 1000)


class ReportBatch(msgspec.Struct, frozen=True):
    """Envelope form of a request body: ``{"reports": [...]}``."""

    reports: list[LocationReport]


class TelemetryRecord(msgspec.Struct, array_like=True, frozen=True, gc=False):
    """A validated report as stored in the TELEMETRY stream (MessagePack array, ~45 bytes)."""

    device_id: str
    recorded_at_ms: int
    received_at_ms: int
    lat: float
    lon: float
    speed: float | None = None
    heading: float | None = None
    accuracy: float | None = None

    @classmethod
    def from_report(cls, report: LocationReport, received_at_ms: int) -> TelemetryRecord:
        heading = report.heading
        if heading is not None and heading >= 360.0:
            heading = 0.0
        return cls(
            device_id=report.device_id,
            recorded_at_ms=report.recorded_at_ms(),
            received_at_ms=received_at_ms,
            lat=report.latitude,
            lon=report.longitude,
            speed=report.speed,
            heading=heading,
            accuracy=report.accuracy,
        )


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def datetime_to_ms(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(aware.timestamp() * 1000)
