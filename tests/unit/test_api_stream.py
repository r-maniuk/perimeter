"""The WebSocket credit protocol against a scripted socket, publisher and admission state."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import msgspec
import pytest

from perimeter.api.ingest.decoding import ReportDecoder
from perimeter.api.ingest.publisher import IngestOverloaded, IngestUnavailable, PublishOutcome
from perimeter.api.ingest.stream import IngestStream
from perimeter.domain.clock import ManualClock
from perimeter.domain.reports import TelemetryRecord
from tests.support import eventually

WINDOW = 10
NOW_MS = 1_790_000_000_000


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed: tuple[int, str | None] | None = None
        self.reading = asyncio.Event()  # cleared: the client stops reading what we send
        self.reading.set()

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_text(self, data: str) -> None:
        await self.reading.wait()
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def send_json(self, message: object) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    def send_binary(self, message: object) -> None:
        self.incoming.put_nowait(
            {"type": "websocket.receive", "bytes": msgspec.msgpack.encode(message)}
        )

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == kind]


class FakePublisher:
    def __init__(self) -> None:
        self.batches: list[list[TelemetryRecord]] = []
        self.gates: list[asyncio.Event] = []
        self.failure: Exception | None = None
        self.rejected: list[str] = []
        self.hold = False

    async def publish(self, records: list[TelemetryRecord]) -> PublishOutcome:
        gate = asyncio.Event()
        if not self.hold:
            gate.set()
        self.batches.append(records)
        self.gates.append(gate)
        await gate.wait()
        if self.failure is not None:
            raise self.failure
        return PublishOutcome(accepted=len(records), duplicates=0)

    def note_rejected(self, codes: list[str]) -> None:
        self.rejected.extend(codes)


class FakeAdmission:
    def __init__(self) -> None:
        self.admitting = True

    def retry_after_s(self) -> int:
        return 7


def report(device: str = "veh-1") -> dict[str, Any]:
    return {"device_id": device, "latitude": 52.37, "longitude": 4.89, "timestamp": NOW_MS}


def frame(seq: int, count: int, *, invalid: int = 0) -> dict[str, Any]:
    reports = [report(f"veh-{i}") for i in range(count)] + [report("bad id")] * invalid
    return {"type": "reports", "seq": seq, "reports": reports}


class Session:
    def __init__(self) -> None:
        self.socket = FakeSocket()
        self.publisher = FakePublisher()
        self.admission = FakeAdmission()
        self.stream = IngestStream(
            cast("Any", self.socket),
            publisher=cast("Any", self.publisher),
            admission=cast("Any", self.admission),
            decoder=ReportDecoder(max_batch=5, max_skew_s=30, max_age_s=7_200),
            window=WINDOW,
            clock=ManualClock(NOW_MS),
            poll_s=0.01,
            send_timeout_s=0.2,
        )
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.stream.serve())

    async def sent(self, count: int) -> list[dict[str, Any]]:
        await eventually(lambda: len(self.socket.sent) >= count, within=2)
        return self.socket.sent

    async def finish(self) -> None:
        assert self.task is not None
        await asyncio.wait_for(self.task, 2)


@pytest.fixture
async def session() -> AsyncIterator[Session]:
    session_ = Session()
    yield session_
    if session_.task is not None and not session_.task.done():
        session_.socket.disconnect()
        for gate in session_.publisher.gates:
            gate.set()
        await session_.finish()


async def test_ready_ack_and_credit_returned(session: Session) -> None:
    session.start()
    assert (await session.sent(1))[0] == {"type": "ready", "credit": WINDOW}
    session.socket.send_json(frame(1, 2, invalid=1))
    ack = (await session.sent(2))[1]
    assert ack["type"] == "ack"
    assert (ack["seq"], ack["accepted"], ack["credit"]) == (1, 2, 3)
    assert [(r["index"], r["code"]) for r in ack["rejected"]] == [(2, "invalid_device_id")]
    assert session.publisher.rejected == ["invalid_device_id"]
    assert session.stream.available == WINDOW


async def test_messagepack_frames_are_understood(session: Session) -> None:
    session.start()
    session.socket.send_binary(frame(5, 3))
    ack = (await session.sent(2))[1]
    assert (ack["type"], ack["seq"], ack["accepted"]) == ("ack", 5, 3)


async def test_acknowledgements_keep_frame_order(session: Session) -> None:
    session.publisher.hold = True
    session.start()
    for seq in (1, 2, 3):
        session.socket.send_json(frame(seq, 2))
    await eventually(lambda: len(session.publisher.gates) == 3)
    assert session.stream.available == WINDOW - 6
    for gate in reversed(session.publisher.gates):  # storage completes in reverse
        gate.set()
    sent = await session.sent(4)
    assert [m["seq"] for m in sent[1:]] == [1, 2, 3]


async def test_exceeding_credit_closes_with_policy_violation(session: Session) -> None:
    session.publisher.hold = True
    session.start()
    session.socket.send_json(frame(1, 5))
    session.socket.send_json(frame(2, 5))
    session.socket.send_json(frame(3, 1))  # 11 > 10
    await eventually(lambda: session.socket.closed is not None)
    assert session.socket.closed == (1008, "credit exceeded")
    for gate in session.publisher.gates:  # the publishes it already started still settle
        gate.set()
    await session.finish()


async def test_bad_frames_get_errors_and_the_socket_stays_open(session: Session) -> None:
    session.start()
    session.socket.incoming.put_nowait({"type": "websocket.receive", "text": "{nope"})
    session.socket.send_json(frame(2, 5) | {"reports": [report()] * 6})  # over the batch limit
    session.socket.send_json(frame(3, 1))
    sent = await session.sent(4)
    malformed, too_large, ack = sent[1:]
    assert (malformed["type"], malformed["code"], malformed["credit"]) == (
        "error",
        "malformed_frame",
        0,
    )
    assert malformed["seq"] is None
    assert (too_large["code"], too_large["seq"], too_large["credit"]) == ("batch_too_large", 2, 6)
    assert (ack["type"], ack["seq"]) == ("ack", 3)
    assert session.stream.available == WINDOW
    assert session.socket.closed is None


async def test_shedding_at_connect_holds_until_admission_reopens(session: Session) -> None:
    session.admission.admitting = False
    session.start()
    ready, hold = await session.sent(2)
    assert ready == {"type": "ready", "credit": 0}
    assert hold == {"type": "hold", "retry_after": 7}
    await asyncio.sleep(0.05)
    assert len(session.socket.sent) == 2  # hold is sent once
    session.admission.admitting = True
    assert (await session.sent(3))[2] == {"type": "credit", "credit": WINDOW}
    assert session.stream.available == WINDOW


async def test_credit_is_withheld_while_shedding_and_returned_at_once(session: Session) -> None:
    session.start()
    await session.sent(1)
    session.admission.admitting = False
    hold = (await session.sent(2))[1]
    assert hold["type"] == "hold"
    session.socket.send_json(frame(1, 3))  # granted credit stays valid while shedding
    session.socket.send_json(frame(2, 2))
    first, second = (await session.sent(4))[2:]
    assert (first["seq"], first["accepted"], first["credit"]) == (1, 3, 0)
    assert (second["seq"], second["credit"]) == (2, 0)
    assert session.stream.withheld == 5
    assert len(session.socket.of_type("hold")) == 1
    session.admission.admitting = True
    assert (await session.sent(5))[4] == {"type": "credit", "credit": 5}
    assert session.stream.available == WINDOW
    assert session.stream.withheld == 0


@pytest.mark.parametrize(
    ("failure", "code", "retry_after"),
    [(IngestUnavailable(), "ingest_unavailable", 7), (IngestOverloaded(), "ingest_overloaded", 1)],
)
async def test_storage_failures_are_errors_that_return_credit(
    session: Session, failure: Exception, code: str, retry_after: int
) -> None:
    session.publisher.failure = failure
    session.start()
    session.socket.send_json(frame(9, 4))
    error = (await session.sent(2))[1]
    assert (error["type"], error["seq"], error["code"]) == ("error", 9, code)
    assert (error["credit"], error["retry_after"]) == (4, retry_after)
    assert session.stream.available == WINDOW


async def test_a_client_that_stops_reading_is_disconnected(session: Session) -> None:
    session.start()
    await session.sent(1)
    session.socket.reading.clear()
    session.socket.send_json(frame(1, 1))
    await session.finish()
    assert session.socket.closed == (1008, "acknowledgements are not being read")


async def test_disconnect_waits_for_started_publishes(session: Session) -> None:
    session.publisher.hold = True
    session.start()
    session.socket.send_json(frame(1, 2))
    await eventually(lambda: len(session.publisher.gates) == 1)
    session.socket.disconnect()
    await asyncio.sleep(0.05)
    assert session.task is not None
    assert not session.task.done()  # still settling the publish it started
    session.publisher.gates[0].set()
    await session.finish()
    assert session.socket.closed is None


async def test_an_unexpected_failure_closes_with_internal_error(session: Session) -> None:
    session.publisher.failure = RuntimeError("storage bug")
    session.start()
    session.socket.send_json(frame(1, 1))
    await session.finish()
    assert session.socket.closed == (1011, "internal error")
