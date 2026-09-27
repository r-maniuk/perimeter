"""The live hub's handling of one socket's end: nothing outlives it, and neither does its token."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketState

from perimeter.api.live.hub import LiveHub, LiveSession
from perimeter.api.live.ops import OpsBoard
from perimeter.api.live.protocol import CloseCode
from perimeter.api.security import Principal
from perimeter.config import load_settings


class FakeSubscription:
    async def unsubscribe(self) -> None:
        return None


class FakeNats:
    stats: dict[str, int] = {"reconnects": 0}  # noqa: RUF012 - read-only, as on the client

    async def subscribe(self, subject: str, cb: Any = None) -> FakeSubscription:
        return FakeSubscription()


class FakeRegistry:
    def on_change(self, listener: Any) -> None:
        return None

    def apply_notice(self, *_: Any) -> None:
        return None


class FakeRevoked:
    def __init__(self) -> None:
        self.revoked: set[str] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        return asyncio.Queue()

    def is_revoked(self, token_id: str) -> bool:
        return token_id in self.revoked


class FakeWebSocket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.headers = {"user-agent": "test"}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.query_params: dict[str, str] = {}
        self.application_state = WebSocketState.CONNECTED
        self.closed_with: int | None = None

    async def send_text(self, data: str) -> None:
        return None

    async def send_bytes(self, data: bytes) -> None:
        return None

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code
        self.application_state = WebSocketState.DISCONNECTED


def received(message: dict[str, Any]) -> dict[str, Any]:
    return {"type": "websocket.receive", "text": json.dumps(message)}


def viewport(west: float) -> dict[str, Any]:
    return received({"type": "viewport", "bbox": [west, 52.30, west + 0.2, 52.45]})


def hub_with(ops: OpsBoard, revoked: FakeRevoked | None = None) -> LiveHub:
    return LiveHub(
        load_settings(),
        nc=FakeNats(),  # type: ignore[arg-type]
        js=None,  # type: ignore[arg-type]
        db=None,  # type: ignore[arg-type]
        registry=FakeRegistry(),  # type: ignore[arg-type]
        ops=ops,
        revoked=revoked or FakeRevoked(),  # type: ignore[arg-type]
        instance="r1",
    )


def open_session(hub: LiveHub, *, expires_at: int) -> tuple[LiveSession, FakeWebSocket]:
    websocket = FakeWebSocket()
    principal = Principal(uuid.uuid4(), "alice", "jti-1", expires_at)
    session = hub._new_session(websocket, principal)  # type: ignore[arg-type]
    hub._sessions[session.sid] = session
    session.conn.start(b'{"type":"hello"}', partial(hub._on_message, session))
    return session, websocket


@pytest.mark.parametrize(
    "last_word", [viewport(5.30), received({"type": "ops", "on": True})], ids=["viewport", "ops"]
)
async def test_a_message_read_during_teardown_sets_nothing_up_again(
    last_word: dict[str, Any],
) -> None:
    ops = OpsBoard(FakeNats())  # type: ignore[arg-type]
    hub = hub_with(ops)

    async def slow_query(_: Any) -> list[Any]:  # a map snapshot still being read
        await asyncio.sleep(10)
        return []

    hub._snapshots._query = slow_query
    session, websocket = open_session(hub, expires_at=2_000_000_000)
    await websocket.inbox.put(viewport(4.80))
    await asyncio.sleep(0.05)
    assert hub._trie.prefixes(session.sid)

    session.close(CloseCode.SIGNED_OUT, "signed out")  # closed by the server ...
    await websocket.inbox.put(last_word)  # ... while the client, unaware, says one more thing
    await hub._finish(session)

    assert not hub._trie.prefixes(session.sid)
    assert not hub._trie.cover
    assert len(ops._viewers) == 0


async def test_a_socket_is_closed_when_its_token_expires() -> None:
    hub = hub_with(OpsBoard(FakeNats()))  # type: ignore[arg-type]
    session, websocket = open_session(hub, expires_at=int(time.time()) + 1)
    await asyncio.wait_for(hub._until_closed_or_expired(session), 3)
    assert session.conn.close_code == CloseCode.SIGNED_OUT
    await hub._finish(session)
    assert websocket.closed_with == CloseCode.SIGNED_OUT


async def test_the_sweep_closes_a_socket_whose_revocation_notice_was_lost() -> None:
    revoked = FakeRevoked()
    hub = hub_with(OpsBoard(FakeNats()), revoked)  # type: ignore[arg-type]
    session, _ = open_session(hub, expires_at=2_000_000_000)
    hub._close_revoked()
    assert not session.conn.closing
    revoked.revoked.add(session.principal.token_id)  # signed out elsewhere, notice dropped
    hub._close_revoked()
    assert session.conn.close_code == CloseCode.SIGNED_OUT
    await hub._finish(session)
