"""Opaque keyset cursors for the list endpoints.

Lists are ordered by ``(timestamp DESC, id DESC)`` and a page continues strictly *after* the last
item the client received, so pages stay stable while rows are inserted and every page is an index
range scan, however deep the client goes. The cursor carries that position: the exact timestamp
(integer microseconds, as PostgreSQL stores it) and the id, MessagePack-encoded and base64url'd
so clients treat it as opaque. It is tagged with the list it belongs to, so a zones cursor handed
to the alerts endpoint is refused rather than silently misread.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import UTC, datetime, timedelta

import msgspec

MAX_CURSOR_CHARS = 128
_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


class CursorError(ValueError):
    """The cursor was not issued for this list (malformed, truncated or edited)."""


class _Payload(msgspec.Struct, array_like=True, frozen=True):
    version: int
    kind: str
    micros: int
    id: bytes


_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(_Payload)


def encode_cursor(kind: str, at: datetime, row_id: uuid.UUID) -> str:
    micros = (at - _EPOCH) // _MICROSECOND
    raw = _encoder.encode(_Payload(_VERSION, kind, micros, row_id.bytes))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(kind: str, cursor: str) -> tuple[datetime, uuid.UUID]:
    """The ``(timestamp, id)`` position a cursor of list ``kind`` stands for."""
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise CursorError("the cursor is empty or too long")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = _decoder.decode(raw)
        at = _EPOCH + payload.micros * _MICROSECOND
        row_id = uuid.UUID(bytes=payload.id)
    except (binascii.Error, msgspec.DecodeError, ValueError, OverflowError) as exc:
        raise CursorError("the cursor is malformed") from exc
    if payload.version != _VERSION or payload.kind != kind:
        raise CursorError("the cursor belongs to a different list")
    return at, row_id
