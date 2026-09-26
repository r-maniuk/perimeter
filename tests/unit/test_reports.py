from datetime import UTC, datetime

import msgspec
import pytest

from perimeter.domain.reports import (
    LocationReport,
    ReportBatch,
    TelemetryRecord,
    datetime_to_ms,
    ms_to_datetime,
)
from perimeter.wire import telemetry

RECEIVED = 1_790_000_000_000
decode = msgspec.json.Decoder(LocationReport).decode


def test_brief_style_payload_is_accepted() -> None:
    report = decode(
        b'{"device_id":"truck-17","latitude":52.37,"longitude":4.89,'
        b'"timestamp":"2026-09-26T19:00:00Z"}'
    )
    assert report.recorded_at_ms(RECEIVED) == datetime_to_ms(datetime(2026, 9, 26, 19, tzinfo=UTC))


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        (b'"2026-09-26T19:00:00+02:00"', 1_790_442_000_000),
        (b'"2026-09-26T19:00:00"', 1_790_449_200_000),  # naive means UTC
        (b"1790449200", 1_790_449_200_000),  # epoch seconds
        (b"1790449200.5", 1_790_449_200_500),
        (b"1790449200123", 1_790_449_200_123),  # epoch milliseconds
    ],
)
def test_timestamp_formats(stamp: bytes, expected: int) -> None:
    report = decode(b'{"device_id":"d","latitude":0,"longitude":0,"timestamp":%s}' % stamp)
    assert report.recorded_at_ms(RECEIVED) == expected


def test_missing_timestamp_uses_the_receive_time() -> None:
    report = decode(b'{"device_id":"d","latitude":0,"longitude":0}')
    assert report.recorded_at_ms(RECEIVED) == RECEIVED


@pytest.mark.parametrize(
    "body",
    [
        b'{"device_id":"bad id","latitude":0,"longitude":0}',
        b'{"device_id":"","latitude":0,"longitude":0}',
        b'{"device_id":"%s","latitude":0,"longitude":0}' % (b"x" * 65),
        b'{"device_id":"a.b","latitude":0,"longitude":0}',
        b'{"device_id":"d","latitude":90.1,"longitude":0}',
        b'{"device_id":"d","latitude":0,"longitude":-180.5}',
        b'{"device_id":"d","latitude":0,"longitude":0,"speed":-1}',
        b'{"device_id":"d","latitude":0,"longitude":0,"heading":361}',
        b'{"device_id":"d","latitude":"0","longitude":0}',
        b'{"latitude":0,"longitude":0}',
    ],
)
def test_invalid_reports_are_rejected(body: bytes) -> None:
    with pytest.raises(msgspec.ValidationError):
        decode(body)


def test_envelope_form_is_supported() -> None:
    batch = msgspec.json.decode(
        b'{"reports":[{"device_id":"a","latitude":1,"longitude":2}]}', type=ReportBatch
    )
    assert batch.reports[0].device_id == "a"


def test_record_round_trips_through_messagepack() -> None:
    report = LocationReport(
        device_id="veh-1", latitude=52.1, longitude=4.2, timestamp=1_790_000_000.0, heading=360.0
    )
    record = TelemetryRecord.from_report(report, RECEIVED)
    assert record.heading == 0.0
    data = telemetry.encode(record)
    assert len(data) < 64
    assert telemetry.decode(data) == record
    assert telemetry.dedup_id(record) == "veh-1:1790000000000"


def test_ms_datetime_conversions() -> None:
    assert datetime_to_ms(ms_to_datetime(RECEIVED)) == RECEIVED
    assert datetime_to_ms(datetime(2026, 1, 1)) == datetime_to_ms(datetime(2026, 1, 1, tzinfo=UTC))
