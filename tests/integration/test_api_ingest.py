"""HTTP ingest: 202 means stored, every refusal path, and shedding before the body is read."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import msgspec
import pytest
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext

from perimeter.api.state import AppState
from perimeter.config import IngestSettings, Settings, load_settings
from perimeter.domain.reports import TelemetryRecord
from perimeter.wire import subjects, telemetry
from tests.integration.conftest import TEST_INGEST_TOKEN
from tests.support import eventually

DEVICE = {"Authorization": f"Bearer {TEST_INGEST_TOKEN}"}
HIGH, LOW = 40, 10


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Small limits so every refusal path is cheap to reach, and a fast admission loop."""
    return load_settings(
        database=settings.database,
        nats=settings.nats,
        security=settings.security,
        telemetry=settings.telemetry,
        engine=settings.engine,
        observability=settings.observability,
        ingest=IngestSettings(
            max_batch=20,
            max_body_bytes=16_384,
            admission_high=HIGH,
            admission_low=LOW,
            admission_sample_ms=50,
        ),
    )


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def report(device: str = "veh-1", **fields: Any) -> dict[str, Any]:
    return {
        "device_id": device,
        "latitude": 52.3731,
        "longitude": 4.8926,
        "timestamp": now_ms(),
        **fields,
    }


async def post(client: httpx.AsyncClient, body: object, **headers: str) -> httpx.Response:
    return await client.post("/v1/telemetry", json=body, headers=DEVICE | headers)


async def stored(js: JetStreamContext) -> list[tuple[str, TelemetryRecord, dict[str, str]]]:
    info = await js.stream_info(subjects.TELEMETRY_STREAM)
    messages = []
    for seq in range(info.state.first_seq, info.state.last_seq + 1):
        message = await js.get_msg(subjects.TELEMETRY_STREAM, seq)
        assert message.subject is not None
        assert message.data is not None
        messages.append(
            (message.subject, telemetry.decode(message.data), dict(message.headers or {}))
        )
    return messages


async def test_accepted_reports_are_stored_with_their_dedup_id(
    client: httpx.AsyncClient, js: JetStreamContext
) -> None:
    stamp = now_ms() - 2_000
    response = await post(
        client,
        {"reports": [report(timestamp=stamp, speed=12.5, heading=90), report("veh-2")]},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": 2, "duplicates": 0, "rejected": []}
    messages = await stored(js)
    assert len(messages) == 2
    subject, record, headers = messages[0]
    assert subjects.device_of(subject) == "veh-1"
    assert 0 <= subjects.partition_of(subject) < 4
    assert (record.recorded_at_ms, record.speed, record.heading) == (stamp, 12.5, 90.0)
    assert record.received_at_ms >= stamp
    assert headers["Nats-Msg-Id"] == f"veh-1:{stamp}"


async def test_a_retried_report_is_stored_once_and_counted_as_a_duplicate(
    client: httpx.AsyncClient, js: JetStreamContext
) -> None:
    # A device whose answer was lost sends the same report again: same device, same timestamp.
    again = report(timestamp=now_ms() - 1_000)
    first = await post(client, [again])
    retry = await post(client, [again, report("veh-2")])
    assert first.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
    assert retry.json() == {"accepted": 2, "duplicates": 1, "rejected": []}
    assert len(await stored(js)) == 2


@pytest.mark.parametrize("shape", ["object", "array", "envelope"])
async def test_every_body_shape(client: httpx.AsyncClient, shape: str) -> None:
    body: object = {"object": report(), "array": [report()], "envelope": {"reports": [report()]}}[
        shape
    ]
    response = await post(client, body)
    assert response.status_code == 202
    assert response.json()["accepted"] == 1


async def test_messagepack_bodies(client: httpx.AsyncClient, js: JetStreamContext) -> None:
    response = await client.post(
        "/v1/telemetry",
        content=msgspec.msgpack.encode([report(), report("veh-2")]),
        headers=DEVICE | {"Content-Type": "application/msgpack"},
    )
    assert response.status_code == 202
    assert len(await stored(js)) == 2


async def test_a_device_id_ending_in_a_newline_is_refused_in_either_encoding(
    client: httpx.AsyncClient, js: JetStreamContext
) -> None:
    # It would become the subject ``tlm.veh-1\n``, which no broker accepts.
    body = [report("veh-1\n")]
    for response in (
        await post(client, body),
        await client.post(
            "/v1/telemetry",
            content=msgspec.msgpack.encode(body),
            headers=DEVICE | {"Content-Type": "application/msgpack"},
        ),
    ):
        assert response.status_code == 422
        rejected = response.json()["rejected"]
        assert [(r["index"], r["code"]) for r in rejected] == [(0, "invalid_device_id")]
    assert (await js.stream_info(subjects.TELEMETRY_STREAM)).state.messages == 0


async def test_a_retried_report_is_stored_once(
    client: httpx.AsyncClient, js: JetStreamContext
) -> None:
    body = [report(timestamp=now_ms())]
    assert (await post(client, body)).json()["accepted"] == 1
    assert (await post(client, body)).json()["accepted"] == 1  # acknowledged as a duplicate
    assert len(await stored(js)) == 1


async def test_both_device_credential_styles(client: httpx.AsyncClient) -> None:
    by_header = await client.post(
        "/v1/telemetry", json=report(), headers={"X-Ingest-Token": TEST_INGEST_TOKEN}
    )
    assert by_header.status_code == 202


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"X-Ingest-Token": "nope"}, {"Authorization": "nope"}],
)
async def test_device_credentials_are_required(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post("/v1/telemetry", json=report(), headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_a_user_session_is_not_a_device_credential(client: httpx.AsyncClient) -> None:
    session = (await client.post("/v1/token", json={"username": "alice"})).json()
    response = await client.post(
        "/v1/telemetry", json=report(), headers={"Authorization": f"Bearer {session['token']}"}
    )
    assert response.status_code == 401


async def test_mixed_batches_accept_the_valid_reports(
    client: httpx.AsyncClient, js: JetStreamContext
) -> None:
    response = await post(
        client,
        [report(), report(latitude=100), report("bad id"), report(timestamp=now_ms() + 3_600_000)],
    )
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 1
    assert [(r["index"], r["code"]) for r in body["rejected"]] == [
        (1, "invalid_latitude"),
        (2, "invalid_device_id"),
        (3, "timestamp_in_future"),
    ]
    assert len(await stored(js)) == 1


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ([report(latitude=91), report(longitude=-200)], 422, "all_rejected"),
        ([], 422, "empty_batch"),
        ([report()] * 21, 413, "batch_too_large"),
    ],
)
async def test_unusable_batches(
    client: httpx.AsyncClient, js: JetStreamContext, body: object, status: int, code: str
) -> None:
    response = await post(client, body)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert (await js.stream_info(subjects.TELEMETRY_STREAM)).state.messages == 0


async def test_all_rejected_explains_every_report(client: httpx.AsyncClient) -> None:
    response = await post(client, [report(latitude=91), {"latitude": 1}])
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] == "all_rejected"
    assert [(r["index"], r["code"]) for r in problem["rejected"]] == [
        (0, "invalid_latitude"),
        (1, "missing_field"),
    ]
    assert all(r["detail"] for r in problem["rejected"])


async def test_the_load_generator_preflight_gets_a_client_error_not_a_5xx(
    client: httpx.AsyncClient,
) -> None:
    """The generator probes with an empty batch to check the endpoint and the token."""
    response = await client.post(
        "/v1/telemetry",
        content=b'{"reports":[]}',
        headers=DEVICE | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "empty_batch"


async def test_every_throttling_answer_carries_retry_after(
    client: httpx.AsyncClient, api: FastAPI, js: JetStreamContext
) -> None:
    """429 and 503 always say when to come back (a 503 without it means "no API here")."""
    state: AppState = api.state.perimeter
    budget = state.publisher.budget
    assert await budget.acquire(budget.capacity, wait_s=0)  # every in-flight slot taken
    try:
        overloaded = await post(client, [report()])
    finally:
        budget.release(budget.capacity)
    assert overloaded.status_code == 429
    assert overloaded.json()["code"] == "ingest_overloaded"
    assert overloaded.headers["retry-after"] == "1"
    await fill_backlog(js, HIGH)
    await eventually(lambda: not state.admission.admitting, within=5)
    shedding = await post(client, [report()])
    assert (shedding.status_code, shedding.json()["code"]) == (503, "ingest_shedding")
    assert int(shedding.headers["retry-after"]) >= 1


async def test_malformed_unsupported_and_oversized_bodies(client: httpx.AsyncClient) -> None:
    malformed = await client.post("/v1/telemetry", content=b"{oops", headers=DEVICE)
    assert (malformed.status_code, malformed.json()["code"]) == (400, "malformed_body")
    plain = await client.post(
        "/v1/telemetry", content=b"x", headers=DEVICE | {"Content-Type": "text/plain"}
    )
    assert (plain.status_code, plain.json()["code"]) == (415, "unsupported_media_type")
    huge = await client.post("/v1/telemetry", content=b"[" + b" " * 20_000 + b"]", headers=DEVICE)
    assert (huge.status_code, huge.json()["code"]) == (413, "payload_too_large")


async def test_a_broker_failure_is_503_and_nothing_is_claimed(
    client: httpx.AsyncClient, api: FastAPI, js: JetStreamContext
) -> None:
    state: AppState = api.state.perimeter
    state.stop.set()  # freeze admission as it is (open): the failure must come from publishing
    await asyncio.sleep(0.1)
    await js.delete_stream(subjects.TELEMETRY_STREAM)
    response = await post(client, [report()])
    assert response.status_code == 503
    assert response.json()["code"] == "ingest_unavailable"
    assert 1 <= int(response.headers["retry-after"]) <= 30


async def fill_backlog(js: JetStreamContext, count: int) -> None:
    for i in range(count):
        record = TelemetryRecord(f"load-{i}", now_ms(), now_ms(), 52.0, 4.0)
        await js.publish(subjects.telemetry(record.device_id), telemetry.encode(record))


async def test_shedding_refuses_before_reading_the_body_and_recovers(
    client: httpx.AsyncClient, api: FastAPI, js: JetStreamContext
) -> None:
    state: AppState = api.state.perimeter
    await fill_backlog(js, HIGH)  # no engine runs in this test: nothing drains
    await eventually(lambda: not state.admission.admitting, within=5)
    body_read = False

    async def body() -> AsyncIterator[bytes]:
        nonlocal body_read
        body_read = True
        yield json.dumps([report()]).encode()

    refused = await client.post("/v1/telemetry", content=body(), headers=DEVICE)
    assert refused.status_code == 503
    assert refused.json()["code"] == "ingest_shedding"
    assert 1 <= int(refused.headers["retry-after"]) <= 30
    assert refused.json()["retry_after"] == int(refused.headers["retry-after"])
    assert not body_read

    await js.purge_stream(subjects.TELEMETRY_STREAM)  # the backlog drained
    await eventually(lambda: state.admission.admitting, within=5)
    assert (await post(client, [report()])).status_code == 202


async def test_heartbeat_carries_the_ingest_metrics(
    client: httpx.AsyncClient, api: FastAPI, nc: NatsClient
) -> None:
    beats: list[dict[str, Any]] = []

    async def collect(msg: Msg) -> None:
        beats.append(json.loads(msg.data))

    await nc.subscribe("sys.metrics.api.>", cb=collect)
    await post(client, [report()])
    await eventually(lambda: beats, within=3)
    beat = beats[-1]
    for key in ("admission", "lag", "ingest_rate", "ingest_rejected_rate", "publish_p99_ms"):
        assert key in beat
    assert beat["admission"] == "open"
    assert beat["service"] == "api"
