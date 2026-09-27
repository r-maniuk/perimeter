"""WebSocket ingest with credit-based flow control (``/v1/telemetry/stream``).

For devices and gateways that report continuously, one socket avoids the per-request cost of HTTP
and gives the server a way to pace the sender without dropping anything: credit. Server messages
are JSON text; clients send JSON text or MessagePack binary frames of the same shape::

    server  {"type":"ready","credit":N}
    client  {"type":"reports","seq":k,"reports":[...]}             costs len(reports) credit
    server  {"type":"ack","seq":k,"accepted":a,"duplicates":d,"rejected":[...],"credit":c}
    server  {"type":"hold","retry_after":s}
    server  {"type":"credit","credit":c}
    server  {"type":"error","seq":k,"code":"...","detail":"...","credit":c}

Credit is a window of reports, and every ``credit`` field is an increment the client adds to what
it may still send. Frames are acknowledged strictly in order, only after JetStream stored their
reports, and each acknowledgement returns the credit its frame used — unless admission control is
shedding. Then credit is withheld: the client receives one ``hold`` with a retry hint, and once the
backlog has drained, one ``credit`` message returns everything that was withheld. Credit already
granted stays valid, so a client never has to drop reports it was allowed to send.

Problems with a frame are answered with ``error`` and never close the socket, with one exception:
sending more than the granted credit violates the protocol and closes it with 1008. An error about a
frame the server could read returns that frame's credit; a frame that could not be read at all
consumed none.

Each socket has one reader and one writer task. The reader validates a frame, starts publishing it
and moves on; the writer awaits the acknowledgements in frame order. Work in flight per socket is
bounded by the credit window and by a short queue of frames.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import msgspec
import structlog
from fastapi import WebSocket
from prometheus_client import Counter, Gauge
from starlette.websockets import WebSocketDisconnect

from perimeter.api.ingest.admission import AdmissionController
from perimeter.api.ingest.decoding import Encoding, FrameError, Rejection, ReportDecoder
from perimeter.api.ingest.publisher import (
    IngestOverloaded,
    IngestUnavailable,
    PublishOutcome,
    TelemetryPublisher,
)
from perimeter.domain.clock import SYSTEM_CLOCK, Clock

log = structlog.get_logger(__name__)

CLOSE_POLICY_VIOLATION = 1008
CLOSE_INTERNAL_ERROR = 1011
MAX_FRAMES_IN_FLIGHT = 64
ADMISSION_POLL_S = 0.25
SEND_TIMEOUT_S = 10.0
CLOSE_TIMEOUT_S = 1.0
OVERLOADED_RETRY_S = 1

CONNECTIONS = Gauge("perimeter_ingest_ws_connections", "Open ingest WebSocket connections")
FRAMES = Counter("perimeter_ingest_ws_frames_total", "Ingest frames answered", ["outcome"])

_encoder = msgspec.json.Encoder()


class CreditExceeded(Exception):  # noqa: N818 - a protocol violation by the client
    """The client sent more reports than it had credit for."""


@dataclass(slots=True)
class _Reply:
    """What the writer owes the client for one frame, in frame order."""

    seq: int | None
    size: int
    rejected: list[Rejection] = field(default_factory=list)
    publish: asyncio.Task[PublishOutcome] | None = None
    error: tuple[str, str] | None = None


class IngestStream:
    """The credit protocol for one accepted socket."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        publisher: TelemetryPublisher,
        admission: AdmissionController,
        decoder: ReportDecoder,
        window: int,
        clock: Clock = SYSTEM_CLOCK,
        poll_s: float = ADMISSION_POLL_S,
        send_timeout_s: float = SEND_TIMEOUT_S,
    ) -> None:
        self._ws = websocket
        self._publisher = publisher
        self._admission = admission
        self._decoder = decoder
        self._window = window
        self._clock = clock
        self._poll_s = poll_s
        self._send_timeout_s = send_timeout_s
        self._available = 0
        self._withheld = 0
        self._holding = False
        self._replies: asyncio.Queue[_Reply] = asyncio.Queue(MAX_FRAMES_IN_FLIGHT)
        self._publishes: set[asyncio.Task[PublishOutcome]] = set()

    @property
    def available(self) -> int:
        """Credit the client holds, as the server accounts it."""
        return self._available

    @property
    def withheld(self) -> int:
        return self._withheld

    async def serve(self) -> None:
        await self._ws.accept()
        CONNECTIONS.inc()
        try:
            await self._session()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("ingest.ws_failed")
            await self._close(CLOSE_INTERNAL_ERROR, "internal error")
        finally:
            CONNECTIONS.dec()
            if self._publishes:  # let started publishes finish; their outcome is logged/metered
                await asyncio.gather(*self._publishes, return_exceptions=True)

    async def _session(self) -> None:
        if self._admission.admitting:
            self._available = self._window
            await self._send({"type": "ready", "credit": self._window})
        else:
            self._withheld = self._window
            await self._send({"type": "ready", "credit": 0})
            await self._hold()
        reader = asyncio.create_task(self._read(), name="ingest-ws-read")
        writer = asyncio.create_task(self._write(), name="ingest-ws-write")
        _, unfinished = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)
        for task in (reader, writer):
            error = None if task.cancelled() else task.exception()
            if isinstance(error, CreditExceeded):
                FRAMES.labels("violation").inc()
                log.info("ingest.ws_credit_exceeded", detail=str(error))
                await self._close(CLOSE_POLICY_VIOLATION, "credit exceeded")
                return
            if isinstance(error, TimeoutError):  # the writer's send deadline
                log.info("ingest.ws_send_timeout")
                await self._close(CLOSE_POLICY_VIOLATION, "acknowledgements are not being read")
                return
            if error is not None:
                raise error

    async def _read(self) -> None:
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            payload: bytes | str = text if text is not None else message.get("bytes") or b""
            encoding = Encoding.JSON if text is not None else Encoding.MSGPACK
            try:
                frame = self._decoder.frame(payload, encoding, received_at_ms=self._clock.now_ms())
            except FrameError as exc:
                size = exc.size or 0
                self._consume(size)
                await self._replies.put(_Reply(exc.seq, size, error=(exc.code, exc.detail)))
                continue
            batch = frame.batch
            self._consume(batch.size)
            if batch.rejected:
                self._publisher.note_rejected([r.code for r in batch.rejected])
            publish = None
            if batch.records:
                publish = asyncio.create_task(self._publisher.publish(batch.records))
                self._publishes.add(publish)
                publish.add_done_callback(self._publishes.discard)
            await self._replies.put(_Reply(frame.seq, batch.size, batch.rejected, publish))

    def _consume(self, size: int) -> None:
        if size > self._available:
            msg = f"a frame of {size} reports with {self._available} credit left"
            raise CreditExceeded(msg)
        self._available -= size

    async def _write(self) -> None:
        while True:
            try:
                reply = await asyncio.wait_for(self._replies.get(), self._poll_s)
            except TimeoutError:
                await self._follow_admission()
                continue
            await self._answer(reply)
            await self._follow_admission()

    async def _answer(self, reply: _Reply) -> None:
        error = reply.error
        accepted = duplicates = 0
        retry_after: int | None = None
        if reply.publish is not None:
            try:
                outcome = await asyncio.shield(reply.publish)
                accepted, duplicates = outcome.accepted, outcome.duplicates
            except IngestOverloaded:
                error = ("ingest_overloaded", "the ingest pipeline is saturated; resend the frame")
                retry_after = OVERLOADED_RETRY_S
            except IngestUnavailable:
                error = ("ingest_unavailable", "the frame was not stored; resend it")
                retry_after = self._admission.retry_after_s()
        credit = self._grant(reply.size)
        if error is not None:
            code, detail = error
            message: dict[str, Any] = {
                "type": "error",
                "seq": reply.seq,
                "code": code,
                "detail": detail,
                "credit": credit,
            }
            if retry_after is not None:
                message["retry_after"] = retry_after
            FRAMES.labels("error").inc()
        else:
            message = {
                "type": "ack",
                "seq": reply.seq,
                "accepted": accepted,
                "duplicates": duplicates,
                "rejected": [
                    {"index": r.index, "code": r.code, "detail": r.detail} for r in reply.rejected
                ],
                "credit": credit,
            }
            FRAMES.labels("ack").inc()
        await self._send(message)

    def _grant(self, size: int) -> int:
        """Credit to return with a reply: all of it, unless admission holds it back."""
        if self._holding or not self._admission.admitting:
            self._withheld += size
            return 0
        self._available += size
        return size

    async def _follow_admission(self) -> None:
        if not self._admission.admitting:
            if not self._holding:
                await self._hold()
        elif self._holding or self._withheld:
            credit, self._withheld, self._holding = self._withheld, 0, False
            self._available += credit
            await self._send({"type": "credit", "credit": credit})

    async def _hold(self) -> None:
        self._holding = True
        await self._send({"type": "hold", "retry_after": self._admission.retry_after_s()})

    async def _send(self, message: dict[str, Any]) -> None:
        async with asyncio.timeout(self._send_timeout_s):
            await self._ws.send_text(_encoder.encode(message).decode())

    async def _close(self, code: int, reason: str) -> None:
        with suppress(Exception):
            async with asyncio.timeout(CLOSE_TIMEOUT_S):
                await self._ws.close(code=code, reason=reason)
