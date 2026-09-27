from datetime import UTC, datetime
from typing import Annotated

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import StringConstraints, TypeAdapter, ValidationError

from perimeter.domain.reports import (
    DEVICE_ID_PATTERN,
    DeviceId,
    LocationReport,
    ReportBatch,
    TelemetryRecord,
    datetime_to_ms,
    ms_to_datetime,
)
from perimeter.wire import telemetry

RECEIVED = 1_790_000_000_000
decode = msgspec.json.Decoder(LocationReport).decode

# A device id as a path or query parameter: fastapi hands the constraints to pydantic.
parameter: TypeAdapter[str] = TypeAdapter(
    Annotated[str, StringConstraints(max_length=64, pattern=DEVICE_ID_PATTERN)]
)


def accepted_in_a_report(device_id: str) -> bool:
    try:
        msgspec.convert(device_id, DeviceId)
    except msgspec.ValidationError:
        return False
    return True


def accepted_as_a_parameter(device_id: str) -> bool:
    try:
        parameter.validate_python(device_id)
    except ValidationError:
        return False
    return True


def test_brief_style_payload_is_accepted() -> None:
    report = decode(
        b'{"device_id":"truck-17","latitude":52.37,"longitude":4.89,'
        b'"timestamp":"2026-09-26T19:00:00Z"}'
    )
    assert report.recorded_at_ms() == datetime_to_ms(datetime(2026, 9, 26, 19, tzinfo=UTC))


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
    assert report.recorded_at_ms() == expected


def test_a_report_without_a_timestamp_is_refused() -> None:
    # Without it a retried report could not be recognised, nor placed in the device's history.
    with pytest.raises(msgspec.ValidationError, match="missing required field `timestamp`"):
        decode(b'{"device_id":"d","latitude":0,"longitude":0}')


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


@pytest.mark.parametrize(
    ("device_id", "accepted"),
    [
        ("dev-00001", True),
        ("A_z-9", True),
        ("x" * 64, True),
        ("dev-00001\n", False),  # would become a broker subject with a newline in it
        ("dev\n00001", False),
        ("dev-00001\r", False),
        ("dev-00001z\n", False),
        ("x" * 65, False),
        ("", False),
    ],
)
def test_a_device_id_must_be_one_subject_token(device_id: str, accepted: bool) -> None:
    assert accepted_in_a_report(device_id) is accepted
    assert accepted_as_a_parameter(device_id) is accepted


@given(
    st.from_regex(r"[A-Za-z0-9_-]{1,65}[\n\r]?", fullmatch=True)
    | st.text(alphabet="aZ09_-.z \n\r\t\x00", max_size=66)
)
def test_reports_and_parameters_accept_the_same_device_ids(device_id: str) -> None:
    # Two engines read the patterns: Python's re for reports (msgspec) and Rust's regex for
    # parameters (pydantic); their "$" differs before a final newline.
    assert accepted_in_a_report(device_id) == accepted_as_a_parameter(device_id)


def test_envelope_form_is_supported() -> None:
    batch = msgspec.json.decode(
        b'{"reports":[{"device_id":"a","latitude":1,"longitude":2,"timestamp":1790000000}]}',
        type=ReportBatch,
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
