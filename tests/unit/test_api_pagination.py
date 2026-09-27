from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st

from perimeter.api.pagination import (
    MAX_CURSOR_CHARS,
    CursorError,
    decode_cursor,
    encode_cursor,
)

AT = datetime(2026, 9, 26, 19, 7, 6, 131_457, tzinfo=UTC)


def test_round_trip_keeps_the_exact_microsecond() -> None:
    row_id = uuid.uuid7()
    cursor = encode_cursor("zones", AT, row_id)
    assert decode_cursor("zones", cursor) == (AT, row_id)
    assert "=" not in cursor
    assert cursor.isascii()


@given(
    at=st.datetimes(
        min_value=datetime(1970, 1, 1),
        max_value=datetime(2200, 1, 1),
        timezones=st.just(UTC),
    ),
    raw=st.binary(min_size=16, max_size=16),
)
def test_round_trip_property(at: datetime, raw: bytes) -> None:
    row_id = uuid.UUID(bytes=raw)
    assert decode_cursor("alerts", encode_cursor("alerts", at, row_id)) == (at, row_id)


def test_a_cursor_of_another_list_is_refused() -> None:
    cursor = encode_cursor("zones", AT, uuid.uuid7())
    with pytest.raises(CursorError, match="different list"):
        decode_cursor("alerts", cursor)


def _forge(payload: object) -> str:
    return base64.urlsafe_b64encode(msgspec.msgpack.encode(payload)).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "x" * (MAX_CURSOR_CHARS + 1),
        "not base64 at all!",
        "é" * 8,
        "AAAA",
        _forge([1, "zones", 0, b"short"]),
        _forge([1, "zones", 2**63 - 1, uuid.uuid7().bytes]),
        _forge({"version": 1}),
        _forge([2, "zones", 0, uuid.uuid7().bytes]),
    ],
)
def test_malformed_cursors_are_refused(cursor: str) -> None:
    with pytest.raises(CursorError):
        decode_cursor("zones", cursor)
