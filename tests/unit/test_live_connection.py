from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from starlette.websockets import WebSocket, WebSocketState

from perimeter.api.live import metrics
from perimeter.api.live.connection import ConnectionClosing, LiveConnection, TokenBucket
from perimeter.api.live.protocol import ClientMessage, Ping, Viewport
from perimeter.wire.frames import FrameKind, FramePoint, decode_bundle, encode_tile
from tests.support import eventually


class FakeWebSocket:
    """Records what is sent; ``stall()`` makes sends hang like a client that stopped reading."""

    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.application_state = WebSocketState.CONNECTED
        self.closed: tuple[int, str] | None = None

    def stall(self) -> None:
        self.flowing.clear()

    def unstall(self) -> None:
        self.flowing.set()

    async def send_text(self, data: str) -> None:
        await self.flowing.wait()
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.flowing.wait()
        self.sent.append(data)

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.application_state = WebSocketState.DISCONNECTED
        await self.flowing.wait()
        self.closed = (code, reason or "")

    def texts(self) -> list[dict[str, Any]]:
        return [json.loads(item) for item in self.sent if isinstance(item, str)]

    def bundles(self) -> list[list[bytes]]:
        return [[bytes(f) for f in decode_bundle(i)] for i in self.sent if isinstance(i, bytes)]


def frame(n: int, points: int = 1) -> bytes:
    return encode_tile(
        FrameKind.LIVE,
        12,
        2105,
        1346,
        [FramePoint(f"dev-{n}-{i}", 52.37, 4.89, 1_790_000_000_000 + i) for i in range(points)],
    )


def text(n: int) -> bytes:
    return b'{"type":"event","seq":%d}' % n


def connection(
    socket: FakeWebSocket,
    *,
    texts: int = 16,
    budget: int = 4_096,
    send_timeout_s: float = 1.0,
    counters: metrics.Counters | None = None,
) -> LiveConnection:
    return LiveConnection(
        cast("WebSocket", socket),
        send_timeout_s=send_timeout_s,
        text_capacity=texts,
        position_budget=budget,
        counters=counters or metrics.Counters(),
        close_timeout_s=0.2,
    )


async def ignore(_: ClientMessage) -> None:
    return None


async def test_first_frame_goes_ahead_of_everything_queued_and_texts_keep_their_order() -> None:
    socket = FakeWebSocket()
    conn = connection(socket)
    conn.offer_text(text(1))
    conn.offer_text(text(2))
    conn.start(b'{"type":"hello"}', ignore)
    conn.offer_text(text(3))
    await eventually(lambda: len(socket.sent) == 4)
    assert [t.get("seq", t["type"]) for t in socket.texts()] == ["hello", 1, 2, 3]
    await conn.shutdown()


async def test_pending_position_frames_are_sent_as_one_bundle_of_the_original_bytes() -> None:
    socket = FakeWebSocket()
    conn = connection(socket)
    frames = [frame(1), frame(2, points=3), frame(3)]
    for f in frames:
        assert conn.offer_position(f)
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: len(socket.sent) == 2)
    assert socket.bundles() == [frames]
    await conn.shutdown()


async def test_position_budget_overflow_drops_the_backlog_pauses_and_asks_for_resync() -> None:
    socket = FakeWebSocket()
    counters = metrics.Counters()
    conn = connection(socket, budget=len(frame(0)) * 3, counters=counters)
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: len(socket.sent) == 1)
    socket.stall()
    assert conn.offer_position(frame(1))  # taken by the writer, which now hangs on the socket
    await eventually(lambda: conn.queued_position_bytes == 0)
    assert all(conn.offer_position(frame(n)) for n in (2, 3, 4))
    assert not conn.offer_position(frame(5))  # over budget: backlog dropped
    assert conn.positions_paused
    assert conn.queued_position_bytes == 0
    assert not conn.offer_position(frame(6))  # paused until the client re-sends its viewport
    assert counters.dropped == 5
    socket.unstall()
    await eventually(lambda: any(t["type"] == "resync" for t in socket.texts()))
    assert socket.texts()[-1] == {"type": "resync", "scope": "positions"}
    assert socket.bundles() == [[frame(1)]]
    assert conn.resume_positions()
    assert conn.offer_position(frame(7))
    await eventually(lambda: len(socket.bundles()) == 2)
    assert socket.bundles()[-1] == [frame(7)]
    assert not conn.closing
    await conn.shutdown()


async def test_a_full_event_lane_closes_with_4008_so_the_client_resumes() -> None:
    socket = FakeWebSocket()
    conn = connection(socket, texts=16)
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: len(socket.sent) == 1)
    socket.stall()
    conn.offer_text(text(0))
    await eventually(lambda: conn.queued_texts == 0)
    for n in range(1, 17):
        assert conn.offer_text(text(n))
    assert not conn.offer_text(text(17))
    assert conn.closing
    assert conn.close_code == 4008
    socket.unstall()
    await conn.shutdown()
    assert socket.closed is not None
    assert socket.closed[0] == 4008


async def test_a_socket_that_does_not_take_a_message_in_time_is_closed_with_1011() -> None:
    socket = FakeWebSocket()
    conn = connection(socket, send_timeout_s=0.05)
    socket.stall()
    conn.start(b'{"type":"hello"}', ignore)
    await asyncio.wait_for(conn.wait_closing(), 2)
    assert conn.close_code == 1011
    await asyncio.wait_for(conn.shutdown(), 2)  # the close frame cannot be sent either: bounded
    assert socket.closed is None


async def test_own_work_waits_for_room_and_leaves_half_the_lane_to_live_traffic() -> None:
    socket = FakeWebSocket()
    conn = connection(socket, texts=16)
    socket.stall()
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: conn.queued_texts == 0)  # hello is in flight, stuck
    for n in range(8):
        await conn.put_text(text(n))
    blocked = asyncio.create_task(conn.put_text(text(8)))
    await asyncio.sleep(0.05)
    assert not blocked.done()
    assert all(conn.offer_text(text(100 + n)) for n in range(8))  # live frames still fit
    socket.unstall()
    await asyncio.wait_for(blocked, 2)
    await eventually(lambda: len(socket.sent) == 18)
    await conn.shutdown()


async def test_an_empty_position_lane_accepts_one_oversized_snapshot() -> None:
    socket = FakeWebSocket()
    conn = connection(socket, budget=64)
    big = frame(1, points=40)
    assert len(big) > 64
    await asyncio.wait_for(conn.put_position(big), 1)
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: socket.bundles() == [[big]])
    await conn.shutdown()


async def test_waiting_work_is_released_when_the_connection_closes() -> None:
    socket = FakeWebSocket()
    conn = connection(socket, texts=16)
    socket.stall()
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: conn.queued_texts == 0)
    for n in range(8):
        await conn.put_text(text(n))
    blocked = asyncio.create_task(conn.put_text(text(8)))
    await asyncio.sleep(0.01)
    conn.close(4001, "signed out")
    with pytest.raises(ConnectionClosing):
        await asyncio.wait_for(blocked, 1)
    with pytest.raises(ConnectionClosing):
        await conn.put_position(frame(1))
    assert not conn.offer_text(text(9))
    socket.unstall()
    await conn.shutdown()
    assert socket.closed == (4001, "signed out")


async def test_the_reader_hands_messages_over_and_closes_on_garbage() -> None:
    socket = FakeWebSocket()
    conn = connection(socket)
    received: list[ClientMessage] = []

    async def handler(message: ClientMessage) -> None:
        received.append(message)

    conn.start(b'{"type":"hello"}', handler, Ping(t=1))
    await socket.inbox.put(
        {"type": "websocket.receive", "text": '{"type":"viewport","bbox":[4.8,52.3,5.0,52.4]}'}
    )
    await eventually(lambda: len(received) == 2)
    assert received[0] == Ping(t=1)
    assert received[1] == Viewport(bbox=(4.8, 52.3, 5.0, 52.4))
    await socket.inbox.put({"type": "websocket.receive", "text": '{"type":"teleport"}'})
    await asyncio.wait_for(conn.wait_closing(), 1)
    assert conn.close_code == 1008
    await conn.shutdown()
    assert socket.closed is not None
    assert socket.closed[0] == 1008


async def test_binary_client_messages_are_a_protocol_error() -> None:
    socket = FakeWebSocket()
    conn = connection(socket)
    conn.start(b'{"type":"hello"}', ignore)
    await socket.inbox.put({"type": "websocket.receive", "bytes": b"\x00"})
    await asyncio.wait_for(conn.wait_closing(), 1)
    assert conn.close_code == 1008
    await conn.shutdown()


async def test_a_client_flooding_messages_is_closed() -> None:
    socket = FakeWebSocket()
    conn = connection(socket)
    conn.start(b'{"type":"hello"}', ignore)
    for _ in range(100):
        socket.inbox.put_nowait({"type": "websocket.receive", "text": '{"type":"ping"}'})
    await asyncio.wait_for(conn.wait_closing(), 1)
    assert conn.close_code == 1008
    await conn.shutdown()


async def test_a_peer_that_leaves_gets_no_close_frame() -> None:
    socket = FakeWebSocket()
    conn = connection(socket)
    conn.start(b'{"type":"hello"}', ignore)
    await socket.inbox.put({"type": "websocket.disconnect", "code": 1001})
    await asyncio.wait_for(conn.wait_closing(), 1)
    assert conn.closed_by_peer
    await conn.shutdown()
    assert socket.closed is None


async def test_shutdown_stops_both_tasks_and_sends_the_requested_close() -> None:
    socket = FakeWebSocket()
    counters = metrics.Counters()
    conn = connection(socket, counters=counters)
    conn.start(b'{"type":"hello"}', ignore)
    await eventually(lambda: counters.sent == 1)
    conn.close(4003, "x" * 300)
    await conn.shutdown()
    assert socket.closed is not None
    code, reason = socket.closed
    assert code == 4003
    assert len(reason.encode()) <= 120
    assert all(task.done() for task in conn._tasks)
    assert not conn.offer_text(text(1))
    assert not conn.offer_position(frame(1))


def test_token_bucket_allows_bursts_then_the_steady_rate() -> None:
    now = [0.0]
    bucket = TokenBucket(2.0, 3, clock=lambda: now[0])
    assert [bucket.allow() for _ in range(4)] == [True, True, True, False]
    now[0] += 0.5
    assert bucket.allow()
    assert not bucket.allow()
    now[0] += 10
    assert [bucket.allow() for _ in range(4)] == [True, True, True, False]
