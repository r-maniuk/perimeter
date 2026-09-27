from __future__ import annotations

import json
import math
from typing import Any

import msgspec
import pytest

from perimeter.api.ingest.decoding import (
    BodyError,
    Encoding,
    FrameError,
    ReportDecoder,
    encoding_for,
)
from perimeter.domain.reports import TelemetryRecord

NOW_MS = 1_790_000_000_000
decoder = ReportDecoder(max_batch=5, max_skew_s=30, max_age_s=7_200)


def report(device: str = "veh-1", **fields: Any) -> dict[str, Any]:
    return {"device_id": device, "latitude": 52.37, "longitude": 4.89, "timestamp": NOW_MS} | fields


def as_json(value: object) -> bytes:
    return json.dumps(value).encode()


def body(payload: bytes, encoding: Encoding = Encoding.JSON) -> Any:
    return decoder.body(payload, encoding, received_at_ms=NOW_MS)


@pytest.mark.parametrize(
    "payload",
    [
        as_json(report()),
        as_json([report()]),
        as_json({"reports": [report()]}),
        b'  \n {"device_id":"veh-1","latitude":52.37,"longitude":4.89,"timestamp":1790000000}',
    ],
)
def test_every_body_shape_is_accepted(payload: bytes) -> None:
    batch = body(payload)
    assert batch.rejected == []
    assert batch.records == [
        TelemetryRecord("veh-1", NOW_MS, NOW_MS, 52.37, 4.89, None, None, None)
    ]


def test_messagepack_bodies_are_accepted() -> None:
    payload = msgspec.msgpack.encode({"reports": [report(timestamp=NOW_MS - 1_000, speed=3.5)]})
    batch = body(payload, Encoding.MSGPACK)
    assert batch.records[0].recorded_at_ms == NOW_MS - 1_000
    assert batch.records[0].speed == 3.5


def test_invalid_reports_are_rejected_individually_with_codes() -> None:
    payload = as_json(
        [
            report("ok-1"),
            {"latitude": 1, "longitude": 2},
            report("bad id"),
            report(latitude=95),
            report(longitude="east"),
            report(timestamp="yesterday"),
        ]
    )
    with pytest.raises(BodyError) as too_many:
        body(payload)
    assert too_many.value.status == 413
    decoder_ = ReportDecoder(max_batch=10, max_skew_s=30, max_age_s=7_200)
    batch = decoder_.body(payload, Encoding.JSON, received_at_ms=NOW_MS)
    assert [r.device_id for r in batch.records] == ["ok-1"]
    assert [(r.index, r.code) for r in batch.rejected] == [
        (1, "missing_field"),
        (2, "invalid_device_id"),
        (3, "invalid_latitude"),
        (4, "invalid_longitude"),
        (5, "invalid_timestamp"),
    ]
    assert "device_id" in batch.rejected[0].detail
    assert batch.size == 6


def test_a_device_id_with_a_trailing_newline_is_rejected() -> None:
    # It would become the subject ``tlm.veh-1\n``, which the broker client refuses.
    batch = body(as_json([report("veh-1\n"), report("veh-2")]))
    assert [(r.index, r.code) for r in batch.rejected] == [(0, "invalid_device_id")]
    assert [r.device_id for r in batch.records] == ["veh-2"]


def test_a_non_object_report_is_an_invalid_report() -> None:
    batch = body(as_json([report(), 42]))
    assert [(r.index, r.code) for r in batch.rejected] == [(1, "invalid_report")]


@pytest.mark.parametrize(
    ("stamp", "code"),
    [
        (NOW_MS + 31_000, "timestamp_in_future"),
        ((NOW_MS + 31_000) / 1000, "timestamp_in_future"),
        (NOW_MS - 7_201_000, "timestamp_too_old"),
        (0, "timestamp_too_old"),
        (-5, "timestamp_too_old"),
        (1e300, "timestamp_in_future"),
    ],
)
def test_implausible_timestamps_are_rejected(stamp: float, code: str) -> None:
    batch = body(as_json([report(timestamp=stamp)]))
    assert [r.code for r in batch.rejected] == [code]
    assert batch.records == []


def test_timestamps_within_the_skew_and_retention_are_kept() -> None:
    batch = body(as_json([report(timestamp=NOW_MS + 29_000), report(timestamp=NOW_MS - 7_199_000)]))
    assert len(batch.records) == 2


@pytest.mark.parametrize("stamp", [math.nan, math.inf, -math.inf])
def test_non_finite_epochs_from_messagepack_are_rejected(stamp: float) -> None:
    batch = body(msgspec.msgpack.encode([report(timestamp=stamp)]), Encoding.MSGPACK)
    assert [r.code for r in batch.rejected] == ["invalid_timestamp"]


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        (b"{not json", 400, "malformed_body"),
        (b"42", 400, "malformed_body"),
        (b'{"reports": 5}', 400, "malformed_body"),
        (b"[]", 422, "empty_batch"),
        (b'{"reports": []}', 422, "empty_batch"),
        (as_json([report()] * 6), 413, "batch_too_large"),
    ],
)
def test_unusable_bodies(payload: bytes, status: int, code: str) -> None:
    with pytest.raises(BodyError) as error:
        body(payload)
    assert (error.value.status, error.value.code) == (status, code)


def frame(payload: bytes | str, encoding: Encoding = Encoding.JSON) -> Any:
    return decoder.frame(payload, encoding, received_at_ms=NOW_MS)


def test_frames_in_json_and_messagepack() -> None:
    message = {"type": "reports", "seq": 7, "reports": [report(), report("bad id")]}
    for decoded in (
        frame(json.dumps(message)),
        frame(msgspec.msgpack.encode(message), Encoding.MSGPACK),
    ):
        assert decoded.seq == 7
        assert decoded.batch.size == 2
        assert [r.code for r in decoded.batch.rejected] == ["invalid_device_id"]


@pytest.mark.parametrize(
    ("message", "code", "seq", "size"),
    [
        ("{nope", "malformed_frame", None, None),
        ({"type": "ping", "seq": 1}, "unsupported_type", 1, None),
        ({"type": "reports", "reports": [{}]}, "invalid_seq", None, None),
        ({"type": "reports", "seq": -1, "reports": [{}]}, "invalid_seq", None, None),
        ({"type": "reports", "seq": 3}, "malformed_frame", 3, None),
        ({"type": "reports", "seq": 3, "reports": []}, "empty_batch", 3, 0),
        ({"type": "reports", "seq": 4, "reports": [report()] * 6}, "batch_too_large", 4, 6),
    ],
)
def test_frame_errors(message: object, code: str, seq: int | None, size: int | None) -> None:
    payload = message if isinstance(message, str) else json.dumps(message)
    with pytest.raises(FrameError) as error:
        frame(payload)
    assert (error.value.code, error.value.seq, error.value.size) == (code, seq, size)


@pytest.mark.parametrize(
    ("content_type", "encoding"),
    [
        (None, Encoding.JSON),
        ("application/json", Encoding.JSON),
        ("application/json; charset=utf-8", Encoding.JSON),
        ("Application/Vnd.Perimeter+JSON", Encoding.JSON),
        ("application/msgpack", Encoding.MSGPACK),
        ("application/x-msgpack", Encoding.MSGPACK),
        ("text/plain", None),
        ("multipart/form-data; boundary=x", None),
    ],
)
def test_content_types(content_type: str | None, encoding: Encoding | None) -> None:
    assert encoding_for(content_type) == encoding
