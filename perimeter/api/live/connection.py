"""One live WebSocket: a reader task, a writer task and two bounded lanes in between.

Producers are NATS callbacks fanning one message out to many sockets, so they must never wait for
a socket. They only ever *offer* frames, which either queue immediately or are refused:

* the **text lane** (events, pulses, session lists, pongs, ops) is bounded by count. Overflowing it
  means the client fell behind its event stream: the connection closes with 4008 and the client
  reconnects with ``resume_after``, replaying exactly what it missed from JetStream.
* the **position lane** is bounded by bytes. Positions are state, not history: on overflow every
  queued frame is dropped, the client is told to ``resync``, and the lane stays paused until the
  client sends its viewport again (which brings fresh snapshots).

Work that belongs to the session itself (replaying events, filling snapshots) *puts* frames
instead: it waits for room and never uses more than half of a lane, so a slow client slows down
its own catch-up without ever being closed for it and without affecting anybody else.

The writer drains everything pending in one turn: text frames in order, then all position frames
as one binary bundle of the tile frames exactly as they came from NATS. Every send is bounded by
``LIVE_SEND_TIMEOUT_S``; a socket that does not take a message in time is closed with 1011.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from collections.abc import Awaitable, Callable
from functools import partial

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from perimeter.api.live import metrics
from perimeter.api.live.protocol import (
    RESYNC_POSITIONS,
    ClientMessage,
    CloseCode,
    ProtocolError,
    decode_client,
)
from perimeter.wire.frames import MAX_BUNDLE_FRAMES, encode_bundle

log = structlog.get_logger(__name__)

CLOSE_TIMEOUT_S = 2.0
MESSAGE_RATE_PER_S = 20.0
MESSAGE_BURST = 40
MAX_REASON_BYTES = 120  # a close frame carries at most 123 bytes of reason

type MessageHandler = Callable[[ClientMessage], Awaitable[None]]


class ConnectionClosing(Exception):  # noqa: N818 - a state of the connection, not a failure
    """The connection is closing, so work queued for it is pointless now."""


class TokenBucket:
    """Rate limit: ``rate`` messages per second on average, bursts of up to ``burst``."""

    def __init__(
        self, rate: float, burst: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._at = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._at) * self._rate)
        self._at = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


def _reason(text: str) -> str:
    return text.encode()[:MAX_REASON_BYTES].decode(errors="ignore")


class LiveConnection:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        send_timeout_s: float,
        text_capacity: int,
        position_budget: int,
        counters: metrics.Counters,
        close_timeout_s: float = CLOSE_TIMEOUT_S,
    ) -> None:
        self._websocket = websocket
        self._send_timeout_s = send_timeout_s
        self._close_timeout_s = close_timeout_s
        self._text_capacity = text_capacity
        self._position_budget = position_budget
        self._counters = counters
        self._texts: deque[bytes] = deque()
        self._positions: deque[bytes] = deque()
        self._position_bytes = 0  # everything queued in the position lane
        self._live_bytes = 0  # the part offered by shared producers: what the budget bounds
        self._positions_paused = False
        self._wake = asyncio.Event()  # something to send, or closing
        self._room = asyncio.Event()  # the writer emptied the lanes, or closing
        self._closing = asyncio.Event()
        self._close_code: int = CloseCode.INTERNAL_ERROR
        self._close_reason = ""
        self._closed_by_peer = False
        self.peer_close_code: int | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # --- state -------------------------------------------------------------------------------

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    @property
    def close_code(self) -> int:
        return self._close_code

    @property
    def closed_by_peer(self) -> bool:
        return self._closed_by_peer

    @property
    def queued_texts(self) -> int:
        return len(self._texts)

    @property
    def queued_position_bytes(self) -> int:
        return self._position_bytes

    @property
    def positions_paused(self) -> bool:
        return self._positions_paused

    # --- producers ---------------------------------------------------------------------------

    def offer_text(self, frame: bytes) -> bool:
        """Queue a text frame without waiting; a full lane closes the connection with 4008."""
        if self._closing.is_set():
            return False
        if len(self._texts) >= self._text_capacity:
            self._drop("text", 1)
            self.close(CloseCode.EVENTS_OVERFLOW, "fell behind the event stream: resume")
            return False
        self._texts.append(frame)
        self._wake.set()
        return True

    def offer_position(self, frame: bytes) -> bool:
        """Queue a live tile frame without waiting; overflowing the budget triggers a resync."""
        if self._closing.is_set():
            return False
        if self._positions_paused:
            self._drop("position", 1)
            return False
        if self._positions and self._live_bytes + len(frame) > self._position_budget:
            self._drop("position", len(self._positions) + 1)
            self._positions.clear()
            self._position_bytes = self._live_bytes = 0
            self._positions_paused = True
            self.offer_text(RESYNC_POSITIONS)
            return False
        self._positions.append(frame)
        self._position_bytes += len(frame)
        self._live_bytes += len(frame)
        self._wake.set()
        return True

    def resume_positions(self) -> bool:
        """Accept position frames again; True if the lane was paused by an overflow."""
        paused = self._positions_paused
        self._positions_paused = False
        return paused

    async def put_text(self, frame: bytes) -> None:
        """Queue a frame of this session's own work, waiting while the lane is half full."""
        while len(self._texts) >= max(1, self._text_capacity // 2):
            await self._wait_for_room()
        self._ensure_open()
        self._texts.append(frame)
        self._wake.set()

    async def put_position(self, frame: bytes) -> None:
        """Queue a snapshot frame, waiting while half the position budget is in use.

        An empty lane always takes the frame, however large, so a big snapshot cannot deadlock.
        """
        while self._positions and self._position_bytes + len(frame) > self._position_budget // 2:
            await self._wait_for_room()
        self._ensure_open()
        if self._positions_paused:  # a resync is pending: the client will ask again
            self._drop("position", 1)
            return
        self._positions.append(frame)
        self._position_bytes += len(frame)
        self._wake.set()

    async def _wait_for_room(self) -> None:
        self._ensure_open()
        self._room.clear()
        await self._room.wait()
        self._ensure_open()

    def _ensure_open(self) -> None:
        if self._closing.is_set():
            raise ConnectionClosing

    def _drop(self, lane: str, frames: int) -> None:
        self._counters.dropped += frames
        metrics.DROPPED_FRAMES.labels(lane).inc(frames)

    # --- lifecycle ---------------------------------------------------------------------------

    def start(
        self, first_frame: bytes, handler: MessageHandler, pending: ClientMessage | None = None
    ) -> None:
        """Send ``first_frame`` ahead of anything queued so far, then run reader and writer."""
        self._texts.appendleft(first_frame)
        self._wake.set()
        self._tasks = [
            asyncio.create_task(self._guard(self._write, "writer"), name="live-writer"),
            asyncio.create_task(
                self._guard(partial(self._read, handler, pending), "reader"), name="live-reader"
            ),
        ]

    def close(self, code: int, reason: str = "") -> None:
        """Ask for the connection to close; the first request decides the code."""
        if self._closing.is_set():
            return
        self._close_code = code
        self._close_reason = _reason(reason)
        self._closing.set()
        self._wake.set()
        self._room.set()

    def mark_peer_closed(self, code: int | None = None) -> None:
        """The client closed or dropped the connection: no close frame will be sent."""
        self._closed_by_peer = True
        self.peer_close_code = code
        self.close(CloseCode.GOING_AWAY, "peer went away")

    async def wait_closing(self) -> None:
        await self._closing.wait()

    async def shutdown(self) -> None:
        """Stop both tasks, then send the close frame; bounded, so a stuck peer cannot stall it."""
        self.close(CloseCode.GOING_AWAY)  # no-op when a code was already chosen
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._closed_by_peer or self._websocket.application_state != WebSocketState.CONNECTED:
            return
        try:
            async with asyncio.timeout(self._close_timeout_s):
                await self._websocket.close(self._close_code, self._close_reason)
        except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
            log.debug("live.close_frame_not_sent", code=self._close_code)

    async def _guard(self, work: Callable[[], Awaitable[None]], role: str) -> None:
        # The coroutine is made here, not by the caller: a connection closed before its tasks
        # first ran cancels them before they start, and must not leave a coroutine never awaited.
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live.connection_task_failed", role=role)
            self.close(CloseCode.INTERNAL_ERROR, "internal error")

    # --- reader ------------------------------------------------------------------------------

    async def _read(self, handler: MessageHandler, pending: ClientMessage | None) -> None:
        if pending is not None:
            await handler(pending)
        limiter = TokenBucket(MESSAGE_RATE_PER_S, MESSAGE_BURST)
        while not self._closing.is_set():
            message = await self._websocket.receive()
            if message["type"] == "websocket.disconnect":
                self.mark_peer_closed(message.get("code"))
                return
            text = message.get("text")
            if text is None:
                self.close(CloseCode.PROTOCOL_ERROR, "binary messages are not part of the protocol")
                return
            if not limiter.allow():
                self.close(CloseCode.PROTOCOL_ERROR, "too many messages")
                return
            try:
                decoded = decode_client(text)
            except ProtocolError as exc:
                self.close(CloseCode.PROTOCOL_ERROR, str(exc))
                return
            await handler(decoded)

    # --- writer ------------------------------------------------------------------------------

    async def _write(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._closing.is_set():
                return
            texts, self._texts = self._texts, deque()
            frames, self._positions = self._positions, deque()
            self._position_bytes = self._live_bytes = 0
            self._room.set()
            for text in texts:
                if not await self._send(text, binary=False):
                    return
            batches = itertools.batched(frames, MAX_BUNDLE_FRAMES, strict=False)
            for batch in batches:
                if not await self._send(encode_bundle(batch), binary=True):
                    return

    async def _send(self, payload: bytes, *, binary: bool) -> bool:
        if self._closing.is_set():
            return False
        try:
            text = None if binary else payload.decode()
        except UnicodeDecodeError:
            log.warning("live.invalid_text_frame_dropped", size=len(payload))
            self._drop("text", 1)
            return True
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._send_timeout_s):
                if text is None:
                    await self._websocket.send_bytes(payload)
                else:
                    await self._websocket.send_text(text)
        except TimeoutError:
            self.close(CloseCode.INTERNAL_ERROR, "send timed out")
            return False
        except (OSError, RuntimeError, WebSocketDisconnect):
            self.mark_peer_closed()
            return False
        metrics.SEND_SECONDS.observe(time.perf_counter() - started)
        metrics.SENT_MESSAGES.labels("binary" if binary else "text").inc()
        metrics.SENT_BYTES.inc(len(payload))
        self._counters.sent += 1
        return True
