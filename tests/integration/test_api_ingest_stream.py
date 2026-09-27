"""WebSocket ingest through a real uvicorn server: credit, acks, errors, hold and resume."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import msgspec
import pytest
import uvicorn
from fastapi import FastAPI
from nats.js import JetStreamContext
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from perimeter.api.state import AppState
from perimeter.config import IngestSettings, Settings, load_settings
from perimeter.domain.clock import SYSTEM_CLOCK
from perimeter.domain.reports import TelemetryRecord
from perimeter.wire import subjects, telemetry
from tests.integration.conftest import TEST_INGEST_TOKEN
from tests.support import eventually

WINDOW = 20
HIGH, LOW = 60, 10
DEVICE = {"Authorization": f"Bearer {TEST_INGEST_TOKEN}"}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return load_settings(
        database=settings.database,
        nats=settings.nats,
        security=settings.security,
        telemetry=settings.telemetry,
        engine=settings.engine,
        observability=settings.observability,
        ingest=IngestSettings(
            max_batch=10,
            ws_initial_credit=WINDOW,
            admission_high=HIGH,
            admission_low=LOW,
            admission_sample_ms=50,
        ),
    )


@pytest.fixture
async def server(api: FastAPI) -> AsyncIterator[str]:
    """The started application behind a real uvicorn server, as in production."""
    config = uvicorn.Config(
        api,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        ws="websockets-sansio",
        ws_max_size=1024 * 1024,
        log_config=None,
        access_log=False,
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    await eventually(lambda: instance.started, within=10)
    port = instance.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/v1/telemetry/stream"
    instance.should_exit = True
    await asyncio.wait_for(task, 10)


def report(device: str = "veh-1", **fields: Any) -> dict[str, Any]:
    return {
        "device_id": device,
        "latitude": 52.3731,
        "longitude": 4.8926,
        "timestamp": SYSTEM_CLOCK.now_ms(),
        **fields,
    }


def reports_frame(seq: int, count: int, *, prefix: str = "veh") -> dict[str, Any]:
    return {
        "type": "reports",
        "seq": seq,
        "reports": [report(f"{prefix}-{i}") for i in range(count)],
    }


async def receive(socket: ClientConnection) -> dict[str, Any]:
    message: dict[str, Any] = json.loads(await asyncio.wait_for(socket.recv(), 5))
    return message


async def test_ready_ack_and_credit_round_trip(server: str, js: JetStreamContext) -> None:
    async with connect(server, additional_headers=DEVICE) as socket:
        assert await receive(socket) == {"type": "ready", "credit": WINDOW}
        frame = reports_frame(1, 3) | {"reports": [report("a"), report("bad id"), report("b")]}
        await socket.send(json.dumps(frame))
        ack = await receive(socket)
        assert ack["type"] == "ack"
        assert (ack["seq"], ack["accepted"], ack["credit"]) == (1, 2, 3)
        assert [(r["index"], r["code"]) for r in ack["rejected"]] == [(1, "invalid_device_id")]
        await socket.send(msgspec.msgpack.encode(reports_frame(2, 4, prefix="bin")))
        ack = await receive(socket)
        assert (ack["seq"], ack["accepted"], ack["credit"]) == (2, 4, 4)
    info = await js.stream_info(subjects.TELEMETRY_STREAM)
    assert info.state.messages == 6


async def test_a_device_id_ending_in_a_newline_is_refused(
    server: str, js: JetStreamContext
) -> None:
    async with connect(server, additional_headers=DEVICE) as socket:
        assert (await receive(socket))["type"] == "ready"
        frame = reports_frame(1, 2) | {"reports": [report("veh-1\n"), report("veh-2")]}
        await socket.send(json.dumps(frame))
        ack = await receive(socket)
        assert (ack["type"], ack["accepted"]) == ("ack", 1)
        assert [(r["index"], r["code"]) for r in ack["rejected"]] == [(0, "invalid_device_id")]
    info = await js.stream_info(subjects.TELEMETRY_STREAM)
    assert info.state.messages == 1


async def test_pipelined_frames_are_acknowledged_in_order(server: str) -> None:
    async with connect(server, additional_headers=DEVICE) as socket:
        await receive(socket)
        for seq in range(1, 6):
            await socket.send(json.dumps(reports_frame(seq, 4, prefix=f"f{seq}")))
        acks = [await receive(socket) for _ in range(5)]
        assert [ack["seq"] for ack in acks] == [1, 2, 3, 4, 5]
        assert sum(ack["credit"] for ack in acks) == 20


async def test_errors_leave_the_socket_open(server: str) -> None:
    async with connect(server, additional_headers=DEVICE) as socket:
        await receive(socket)
        await socket.send("{broken")
        malformed = await receive(socket)
        assert (malformed["type"], malformed["code"], malformed["credit"]) == (
            "error",
            "malformed_frame",
            0,
        )
        await socket.send(json.dumps({"type": "ping", "seq": 4}))
        assert (await receive(socket))["code"] == "unsupported_type"
        await socket.send(json.dumps(reports_frame(5, 11)))  # over the batch limit
        too_large = await receive(socket)
        assert (too_large["code"], too_large["seq"], too_large["credit"]) == (
            "batch_too_large",
            5,
            11,
        )
        await socket.send(json.dumps(reports_frame(6, 1)))
        assert (await receive(socket))["type"] == "ack"


async def test_sending_beyond_credit_closes_with_1008(server: str, js: JetStreamContext) -> None:
    async with connect(server, additional_headers=DEVICE) as socket:
        await receive(socket)
        # One frame larger than the whole window: a violation however fast acks come back.
        # (Credit accounting across pipelined frames is covered by the unit tests.)
        await socket.send(json.dumps(reports_frame(1, WINDOW + 1)))
        with pytest.raises(ConnectionClosed) as closed:
            await receive(socket)
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 1008
        assert closed.value.rcvd.reason == "credit exceeded"
    assert (await js.stream_info(subjects.TELEMETRY_STREAM)).state.messages == 0


async def test_a_wrong_token_gets_a_real_http_401(server: str) -> None:
    with pytest.raises(InvalidStatus) as refused:
        async with connect(server, additional_headers={"Authorization": "Bearer nope"}):
            pass
    response = refused.value.response
    assert response.status_code == 401
    assert json.loads(response.body or b"{}")["code"] == "unauthorized"


async def fill_backlog(js: JetStreamContext, count: int) -> None:
    now_ms = time.time_ns() // 1_000_000
    for i in range(count):
        record = TelemetryRecord(f"load-{i}", now_ms, now_ms, 52.0, 4.0)
        await js.publish(subjects.telemetry(record.device_id), telemetry.encode(record))


async def test_credit_is_held_while_shedding_and_returned_when_drained(
    server: str, api: FastAPI, js: JetStreamContext
) -> None:
    state: AppState = api.state.perimeter
    async with connect(server, additional_headers=DEVICE) as socket:
        assert (await receive(socket))["credit"] == WINDOW
        await fill_backlog(js, HIGH)
        await eventually(lambda: not state.admission.admitting, within=5)
        hold = await receive(socket)
        assert hold["type"] == "hold"
        assert 1 <= hold["retry_after"] <= 30
        await socket.send(json.dumps(reports_frame(1, 5)))  # granted credit is still honoured
        ack = await receive(socket)
        assert (ack["type"], ack["accepted"], ack["credit"]) == ("ack", 5, 0)
        await js.purge_stream(subjects.TELEMETRY_STREAM)
        resumed = await receive(socket)
        assert resumed == {"type": "credit", "credit": 5}


async def test_connecting_while_shedding_starts_without_credit(
    server: str, api: FastAPI, js: JetStreamContext
) -> None:
    state: AppState = api.state.perimeter
    await fill_backlog(js, HIGH)
    await eventually(lambda: not state.admission.admitting, within=5)
    async with connect(server, additional_headers=DEVICE) as socket:
        assert await receive(socket) == {"type": "ready", "credit": 0}
        assert (await receive(socket))["type"] == "hold"
        await js.purge_stream(subjects.TELEMETRY_STREAM)
        assert await receive(socket) == {"type": "credit", "credit": WINDOW}


async def test_a_client_spending_only_its_incremental_credit_is_never_cut_off(
    server: str, js: JetStreamContext
) -> None:
    """The load generator's accounting: ready, ack and credit all add to the allowance."""
    total, sent, acked, seq = 95, 0, 0, 0
    async with connect(server, additional_headers=DEVICE) as socket:
        allowance = (await receive(socket))["credit"]
        in_flight: dict[int, int] = {}
        while acked < total:
            while sent < total and allowance > 0:
                size = min(allowance, 10, total - sent)  # never more than what is left
                seq += 1
                reports = [report(f"c{sent + i}") for i in range(size)]
                await socket.send(json.dumps({"type": "reports", "seq": seq, "reports": reports}))
                in_flight[seq] = size
                allowance -= size
                sent += size
            message = await receive(socket)
            allowance += message.get("credit", 0)
            if message["type"] == "ack":
                assert message["seq"] == min(in_flight)  # in order
                acked += in_flight.pop(message["seq"])
        assert allowance == WINDOW  # everything granted came back
    assert (await js.stream_info(subjects.TELEMETRY_STREAM)).state.messages == total


async def test_a_frame_the_broker_did_not_store_is_an_error_that_refunds_its_credit(
    server: str, api: FastAPI, js: JetStreamContext
) -> None:
    state: AppState = api.state.perimeter
    state.stop.set()  # freeze admission as it is (open): the failure must come from publishing
    await asyncio.sleep(0.1)
    async with connect(server, additional_headers=DEVICE) as socket:
        await receive(socket)
        await js.delete_stream(subjects.TELEMETRY_STREAM)
        await socket.send(json.dumps(reports_frame(7, 3)))
        error = await receive(socket)
        assert (error["type"], error["seq"], error["code"]) == ("error", 7, "ingest_unavailable")
        assert error["credit"] == 3
        assert error["retry_after"] >= 1
