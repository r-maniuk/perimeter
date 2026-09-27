"""Device ingest: ``POST /v1/telemetry`` and the WebSocket ``/v1/telemetry/stream``.

The HTTP handler is ordered so that refusing work is cheap: the device credential is checked and
admission control consulted before a single byte of the body is read, so a shedding replica
answers 503 without paying for the upload. Accepted means stored — the 202 is sent only after
JetStream acknowledged every valid report of the batch.
"""

from __future__ import annotations

from typing import Any

import msgspec
from fastapi import APIRouter, Request, Response, WebSocket, status

from perimeter.api.deps import State
from perimeter.api.errors import ProblemError, unauthorized
from perimeter.api.ingest.decoding import BodyError, Rejection, ReportDecoder, encoding_for
from perimeter.api.ingest.publisher import IngestOverloaded, IngestUnavailable
from perimeter.api.ingest.stream import IngestStream
from perimeter.api.schemas import IngestResult
from perimeter.api.security import device_credential, ingest_token_valid
from perimeter.api.state import AppState, state_of
from perimeter.config import Settings
from perimeter.domain.clock import SYSTEM_CLOCK
from perimeter.domain.reports import LocationReport

router = APIRouter(tags=["ingest"])

OVERLOADED_RETRY_S = 1

_encoder = msgspec.json.Encoder()


def _report_schema() -> dict[str, Any]:
    schema = msgspec.json.schema(LocationReport)
    report: dict[str, Any] = schema["$defs"]["LocationReport"]
    return report


_REPORT = _report_schema()
_BODY_SCHEMA = {
    "oneOf": [
        _REPORT,
        {"type": "array", "items": _REPORT, "minItems": 1},
        {
            "type": "object",
            "properties": {"reports": {"type": "array", "items": _REPORT, "minItems": 1}},
            "required": ["reports"],
        },
    ]
}
_REQUEST_BODY = {
    "required": True,
    "description": (
        'One report, an array of reports, or `{"reports": [...]}` — JSON, or MessagePack with '
        "`Content-Type: application/msgpack`. Authenticate with `Authorization: Bearer "
        "<ingest token>` or `X-Ingest-Token`."
    ),
    "content": {
        "application/json": {
            "schema": _BODY_SCHEMA,
            "example": {
                "reports": [
                    {
                        "device_id": "veh-00042",
                        "latitude": 52.3731,
                        "longitude": 4.8926,
                        "timestamp": "2026-09-26T19:07:06.131Z",
                        "speed": 11.4,
                        "heading": 87.5,
                    }
                ]
            },
        },
        "application/msgpack": {"schema": _BODY_SCHEMA},
    },
}


def report_decoder(settings: Settings) -> ReportDecoder:
    return ReportDecoder(
        max_batch=settings.ingest.max_batch,
        max_skew_s=settings.ingest.max_skew_s,
        max_age_s=settings.telemetry.max_age_s,
    )


def _authenticate_device(state: AppState, connection: Request | WebSocket) -> None:
    expected = state.settings.security.ingest_token.get_secret_value()
    if not ingest_token_valid(device_credential(connection), expected):
        raise unauthorized("a valid device ingest token is required")


def _rejections(rejected: list[Rejection]) -> list[dict[str, Any]]:
    return [{"index": r.index, "code": r.code, "detail": r.detail} for r in rejected]


@router.post(
    "/telemetry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResult,
    openapi_extra={"requestBody": _REQUEST_BODY},
    responses={
        401: {"description": "Missing or wrong device ingest token"},
        413: {"description": "Body over `INGEST_MAX_BODY_BYTES` or batch over `INGEST_MAX_BATCH`"},
        415: {"description": "Neither JSON nor MessagePack"},
        422: {"description": "No valid report in the batch (`rejected` lists why)"},
        429: {"description": "In-flight publish budget exhausted; honour `Retry-After`"},
        503: {"description": "Pipeline shedding load or broker unavailable; honour `Retry-After`"},
    },
    summary="Ingest location reports (202 = durably stored)",
)
async def ingest(request: Request, state: State) -> Response:
    _authenticate_device(state, request)
    if not state.admission.admitting:
        retry_after = state.admission.retry_after_s()
        raise ProblemError(
            503,
            "ingest_shedding",
            "the pipeline is working through a backlog; retry later",
            headers={"Retry-After": str(retry_after)},
            extra={"retry_after": retry_after},
        )
    encoding = encoding_for(request.headers.get("content-type"))
    if encoding is None:
        raise ProblemError(
            415, "unsupported_media_type", "send application/json or application/msgpack"
        )
    received_at_ms = SYSTEM_CLOCK.now_ms()
    payload = await request.body()
    try:
        batch = report_decoder(state.settings).body(
            payload, encoding, received_at_ms=received_at_ms
        )
    except BodyError as exc:
        raise ProblemError(exc.status, exc.code, exc.detail) from exc
    if batch.rejected:
        state.publisher.note_rejected([r.code for r in batch.rejected])
    if not batch.records:
        raise ProblemError(
            422,
            "all_rejected",
            "no report in the batch is valid",
            extra={"rejected": _rejections(batch.rejected)},
        )
    try:
        outcome = await state.publisher.publish(batch.records)
    except IngestOverloaded as exc:
        raise ProblemError(
            429,
            "ingest_overloaded",
            "too many reports are awaiting storage on this replica; retry shortly",
            headers={"Retry-After": str(OVERLOADED_RETRY_S)},
            extra={"retry_after": OVERLOADED_RETRY_S},
        ) from exc
    except IngestUnavailable as exc:
        retry_after = state.admission.retry_after_s()
        raise ProblemError(
            503,
            "ingest_unavailable",
            "the telemetry stream did not store the batch; retry it",
            headers={"Retry-After": str(retry_after)},
            extra={"retry_after": retry_after},
        ) from exc
    body = {"accepted": outcome.accepted, "rejected": _rejections(batch.rejected)}
    return Response(
        _encoder.encode(body), status_code=status.HTTP_202_ACCEPTED, media_type="application/json"
    )


@router.websocket("/telemetry/stream")
async def ingest_stream(websocket: WebSocket) -> None:
    """Credit-based streaming ingest (protocol: :mod:`perimeter.api.ingest.stream`)."""
    state = state_of(websocket)
    _authenticate_device(state, websocket)  # before accept: rejected with a real HTTP 401
    stream = IngestStream(
        websocket,
        publisher=state.publisher,
        admission=state.admission,
        decoder=report_decoder(state.settings),
        window=state.settings.ingest.ws_initial_credit,
    )
    await stream.serve()
