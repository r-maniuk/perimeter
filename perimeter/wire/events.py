"""Durable user events: the JSON envelope stored in the EVENTS stream and pushed to sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import msgspec

from perimeter.domain.presence import Transition
from perimeter.domain.reports import ms_to_datetime


class EventType(StrEnum):
    ALERT = "alert"
    ZONE_CREATED = "zone.created"
    ZONE_UPDATED = "zone.updated"
    ZONE_DELETED = "zone.deleted"


class Event(msgspec.Struct, kw_only=True, frozen=True):
    id: str
    type: EventType
    ts: datetime
    data: dict[str, Any]


_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(Event)


def new_id() -> uuid.UUID:
    """Time-ordered identifier (UUIDv7), also used as the JetStream de-duplication id."""
    return uuid.uuid7()


def make_event(
    event_type: EventType,
    data: dict[str, Any],
    *,
    event_id: uuid.UUID | None = None,
    ts: datetime | None = None,
) -> Event:
    return Event(
        id=str(event_id or new_id()),
        type=event_type,
        ts=ts or datetime.now(UTC),
        data=data,
    )


def alert_data(alert_id: uuid.UUID, transition: Transition) -> dict[str, Any]:
    return {
        "alert_id": str(alert_id),
        "kind": transition.kind.value,
        "device_id": transition.device_id,
        "zone": {"id": str(transition.zone.zone_id), "name": transition.zone.name},
        "occurred_at": ms_to_datetime(transition.occurred_at_ms).isoformat(),
        "position": {"lat": transition.lat, "lon": transition.lon},
    }


def encode_event(event: Event) -> bytes:
    return _encoder.encode(event)


def decode_event(data: bytes) -> Event:
    return _decoder.decode(data)


def live_frame(seq: int, prev: int, payload: bytes) -> bytes:
    """WebSocket text frame for one event, built without re-encoding the stored payload."""
    return b'{"type":"event","seq":%d,"prev":%d,"event":%s}' % (seq, prev, payload)
