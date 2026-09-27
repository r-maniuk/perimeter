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

    async def publish(
        self, subject: str, payload: bytes, headers: Mapping[str, str] | None = None
    ) -> asyncio.Future[Ack]:
        self.published.append(subject)
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


async def test_nothing_to_relay_touches_nothing() -> None:
    relay = OutboxRelay(UnreachableDatabase(), AckingStream())  # type: ignore[arg-type]
    assert await relay.relay([]) == 0
