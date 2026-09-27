"""The outbox relay's fast path: what it cannot finish is left to the sweeper, never raised."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from perimeter.bus.publish import Ack
from perimeter.bus.relay import OutboxRelay
from perimeter.storage.outbox import OutboxRow


class AckingStream:
    def __init__(self) -> None:
        self.published: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def publish(
        self, subject: str, payload: bytes, headers: Mapping[str, str] | None = None
    ) -> asyncio.Future[Ack]:
        self.published.append(subject)
        self.headers.append(dict(headers or {}))
        future: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        future.set_result(Ack("EVENTS", len(self.published)))
        return future


class UnreachableDatabase:
    """``begin()`` fails the way a dropped connection or an exhausted pool does."""

    def begin(self) -> UnreachableDatabase:
        return self

    async def __aenter__(self) -> None:
        raise ConnectionResetError("connection to the database was lost")

    async def __aexit__(self, *exc: Any) -> None:
        return None


ROWS = [OutboxRow(1, "evt.u1", "e1", b"{}"), OutboxRow(2, "evt.u1", "e2", b"{}")]


async def test_a_failed_delete_after_publishing_is_left_to_the_sweeper() -> None:
    stream = AckingStream()
    relay = OutboxRelay(UnreachableDatabase(), stream)  # type: ignore[arg-type]
    assert await relay.relay(ROWS) == 2  # published; the committed change is not undone by this
    assert stream.published == ["evt.u1", "evt.u1"]


async def test_events_go_out_with_the_trace_context_they_were_written_in() -> None:
    # Whichever path relays a row, and in whatever span: the row says which trace it is part of.
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    traced = OutboxRow(3, "evt.u1", "e3", b"{}", {"traceparent": traceparent})
    stream = AckingStream()
    relay = OutboxRelay(UnreachableDatabase(), stream)  # type: ignore[arg-type]
    assert await relay.relay([traced, ROWS[0]]) == 2
    assert stream.headers == [
        {"traceparent": traceparent, "Nats-Msg-Id": "e3"},
        {"Nats-Msg-Id": "e1"},
    ]


async def test_nothing_to_relay_touches_nothing() -> None:
    relay = OutboxRelay(UnreachableDatabase(), AckingStream())  # type: ignore[arg-type]
    assert await relay.relay([]) == 0
