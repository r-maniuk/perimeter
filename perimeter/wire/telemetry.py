"""MessagePack encoding of telemetry records carried by the TELEMETRY stream."""

from __future__ import annotations

import msgspec

from perimeter.domain.reports import TelemetryRecord

_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(TelemetryRecord)

DecodeError = msgspec.DecodeError


def encode(record: TelemetryRecord) -> bytes:
    return _encoder.encode(record)


def decode(data: bytes) -> TelemetryRecord:
    return _decoder.decode(data)


def dedup_id(record: TelemetryRecord) -> str:
    """JetStream message id: the same report retried by a device is stored once."""
    return f"{record.device_id}:{record.recorded_at_ms}"
