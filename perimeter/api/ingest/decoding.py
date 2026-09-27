"""Decoding and validation of device reports, shared by the HTTP and WebSocket transports.

Payloads are decoded by msgspec, in C, in two cheap passes: a structural pass that only finds where
each report starts and ends (``msgspec.Raw``), then one typed decode per report. A malformed report
therefore costs its sender exactly that report — rejected with its index, a stable code and the
decoder's explanation — while the rest of the batch is accepted.

Accepted shapes, as JSON or as MessagePack with the same structure:

* one report ``{...}``;
* an array of reports ``[{...}, ...]``;
* an envelope ``{"reports": [{...}, ...]}``;
* on the WebSocket, frames ``{"type": "reports", "seq": k, "reports": [...]}``.

Beyond the schema, a timestamp must be plausible: not further ahead than the allowed clock skew,
and not older than the telemetry retention. A report older than that could never be replayed or
drawn on a trail, and in practice it is a device clock or a unit error (seconds sent as
milliseconds, a clock reset to 1970).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import msgspec
from msgspec import Raw

from perimeter.domain.reports import LocationReport, TelemetryRecord


class Encoding(StrEnum):
    JSON = "json"
    MSGPACK = "msgpack"


MSGPACK_MEDIA_TYPES = frozenset(
    {"application/msgpack", "application/x-msgpack", "application/vnd.msgpack"}
)

_FIELD_CODES = {
    "device_id": "invalid_device_id",
    "latitude": "invalid_latitude",
    "longitude": "invalid_longitude",
    "timestamp": "invalid_timestamp",
    "speed": "invalid_speed",
    "heading": "invalid_heading",
    "accuracy": "invalid_accuracy",
}


class _Body(msgspec.Struct):
    """An object body: an envelope when it has ``reports``, otherwise a single report."""

    reports: list[Raw] | None = None


class _Frame(msgspec.Struct):
    type: str | None = None
    seq: int | None = None
    reports: list[Raw] | None = None


_BODY_TYPE: Any = list[Raw] | _Body

_DECODERS: dict[Encoding, tuple[Any, Any, Any]] = {
    Encoding.JSON: (
        msgspec.json.Decoder(_BODY_TYPE),
        msgspec.json.Decoder(_Frame),
        msgspec.json.Decoder(LocationReport),
    ),
    Encoding.MSGPACK: (
        msgspec.msgpack.Decoder(_BODY_TYPE),
        msgspec.msgpack.Decoder(_Frame),
        msgspec.msgpack.Decoder(LocationReport),
    ),
}


@dataclass(frozen=True, slots=True)
class Rejection:
    index: int
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Batch:
    records: list[TelemetryRecord]
    rejected: list[Rejection]

    @property
    def size(self) -> int:
        return len(self.records) + len(self.rejected)


@dataclass(frozen=True, slots=True)
class Frame:
    seq: int
    batch: Batch


class BodyError(Exception):
    """The payload as a whole is not a batch of reports; none of it was considered."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class FrameError(Exception):
    """A WebSocket frame that cannot be processed.

    ``size`` is the number of reports the frame carried when the frame was readable enough to
    count them (it then consumed that much credit); ``None`` when it was not.
    """

    def __init__(self, code: str, detail: str, *, seq: int | None, size: int | None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.seq = seq
        self.size = size


def encoding_for(content_type: str | None) -> Encoding | None:
    """The payload encoding named by a ``Content-Type`` (JSON when absent), ``None`` if unknown."""
    if not content_type:
        return Encoding.JSON
    media = content_type.partition(";")[0].strip().lower()
    if media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    ):
        return Encoding.JSON
    if media in MSGPACK_MEDIA_TYPES:
        return Encoding.MSGPACK
    return None


class ReportDecoder:
    def __init__(self, *, max_batch: int, max_skew_s: float, max_age_s: float) -> None:
        self.max_batch = max_batch
        self._max_skew_ms = round(max_skew_s * 1000)
        self._max_age_ms = round(max_age_s * 1000)
        self._max_age_s = max_age_s

    def body(self, payload: bytes, encoding: Encoding, *, received_at_ms: int) -> Batch:
        """A request body; raises :class:`BodyError` when it is not a usable batch."""
        body_decoder, _, _ = _DECODERS[encoding]
        try:
            shape = body_decoder.decode(payload)
        except msgspec.DecodeError as exc:
            raise BodyError(400, "malformed_body", f"the body cannot be decoded: {exc}") from exc
        if isinstance(shape, _Body):
            raws = shape.reports if shape.reports is not None else [Raw(payload)]
        else:
            raws = shape
        if not raws:
            raise BodyError(422, "empty_batch", "the body contains no reports")
        if len(raws) > self.max_batch:
            raise BodyError(
                413,
                "batch_too_large",
                f"{len(raws)} reports in one request; the limit is {self.max_batch}",
            )
        return self._validate(raws, encoding, received_at_ms)

    def frame(self, payload: bytes | str, encoding: Encoding, *, received_at_ms: int) -> Frame:
        """A WebSocket frame; raises :class:`FrameError` when it cannot be processed."""
        _, frame_decoder, _ = _DECODERS[encoding]
        try:
            frame = frame_decoder.decode(payload)
        except msgspec.DecodeError as exc:
            detail = f"the frame cannot be decoded: {exc}"
            raise FrameError("malformed_frame", detail, seq=None, size=None) from exc
        seq = frame.seq
        if frame.type != "reports":
            detail = f"unsupported frame type {frame.type!r}; expected 'reports'"
            raise FrameError("unsupported_type", detail, seq=seq, size=None)
        if seq is None or seq < 0:
            detail = "every frame needs a non-negative integer 'seq'"
            raise FrameError("invalid_seq", detail, seq=None, size=None)
        if frame.reports is None:
            detail = "a reports frame needs a 'reports' array"
            raise FrameError("malformed_frame", detail, seq=seq, size=None)
        size = len(frame.reports)
        if size == 0:
            raise FrameError("empty_batch", "the frame contains no reports", seq=seq, size=0)
        if size > self.max_batch:
            detail = f"{size} reports in one frame; the limit is {self.max_batch}"
            raise FrameError("batch_too_large", detail, seq=seq, size=size)
        return Frame(seq, self._validate(frame.reports, encoding, received_at_ms))

    def _validate(self, raws: Sequence[Raw], encoding: Encoding, received_at_ms: int) -> Batch:
        _, _, report_decoder = _DECODERS[encoding]
        records: list[TelemetryRecord] = []
        rejected: list[Rejection] = []
        newest_allowed = received_at_ms + self._max_skew_ms
        oldest_allowed = received_at_ms - self._max_age_ms
        for index, raw in enumerate(raws):
            try:
                report: LocationReport = report_decoder.decode(raw)
            except msgspec.DecodeError as exc:
                rejected.append(_schema_rejection(index, exc))
                continue
            try:
                recorded_at_ms = report.recorded_at_ms(received_at_ms)
            except ValueError, OverflowError:  # NaN or infinite epoch (possible in MessagePack)
                detail = "timestamp must be a finite epoch number or an RFC 3339 string"
                rejected.append(Rejection(index, "invalid_timestamp", detail))
                continue
            if recorded_at_ms > newest_allowed:
                ahead_s = (recorded_at_ms - received_at_ms) / 1000
                detail = (
                    f"timestamp is {ahead_s:.1f} s ahead of the server clock "
                    f"(allowed skew {self._max_skew_ms / 1000:g} s)"
                )
                rejected.append(Rejection(index, "timestamp_in_future", detail))
                continue
            if recorded_at_ms < oldest_allowed:
                detail = f"timestamp is older than the {self._max_age_s:g} s telemetry retention"
                rejected.append(Rejection(index, "timestamp_too_old", detail))
                continue
            records.append(TelemetryRecord.from_report(report, received_at_ms))
        return Batch(records, rejected)


def _schema_rejection(index: int, error: msgspec.DecodeError) -> Rejection:
    detail = str(error)
    if detail.startswith("Object missing required field"):
        return Rejection(index, "missing_field", detail)
    _, marker, path = detail.rpartition(" - at `$.")
    code = _FIELD_CODES.get(path.rstrip("`"), "invalid_report") if marker else "invalid_report"
    return Rejection(index, code, detail)
