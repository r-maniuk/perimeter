from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException
from starlette.types import Message, Receive, Scope, Send

from perimeter.api.bodylimit import BodyLimitMiddleware
from perimeter.api.errors import database_unavailable


class _DriverError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("driver error")
        self.sqlstate = sqlstate


def dbapi_error(sqlstate: str | None, *, invalidated: bool = False) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, _DriverError(sqlstate), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    ("sqlstate", "unavailable"),
    [
        ("40P01", True),  # deadlock victim
        ("40001", True),  # serialization failure
        ("57014", True),  # statement timeout
        ("53300", True),  # too many connections
        ("08006", True),  # connection failure
        ("23505", False),  # unique violation: a bug, not load
        ("22001", False),
        (None, False),
    ],
)
def test_database_errors_are_classified(sqlstate: str | None, unavailable: bool) -> None:
    assert database_unavailable(dbapi_error(sqlstate)) is unavailable


def test_pool_exhaustion_and_lost_connections_are_unavailable() -> None:
    assert database_unavailable(PoolTimeoutError("QueuePool limit reached"))
    assert database_unavailable(dbapi_error(None, invalidated=True))
    assert not database_unavailable(RuntimeError("unrelated"))


class _Recorder:
    """A downstream ASGI app that reads the whole body and records what reached it."""

    def __init__(self) -> None:
        self.body = b""
        self.called = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        if scope["type"] == "http":
            while True:
                message = await receive()
                self.body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})


async def run(
    middleware: BodyLimitMiddleware, headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> list[Message]:
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    await middleware(scope, receive, send)
    return sent


async def test_a_declared_oversized_body_never_reaches_the_app() -> None:
    app = _Recorder()
    sent = await run(BodyLimitMiddleware(app, max_bytes=10), [(b"content-length", b"11")], [b""])
    assert not app.called
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["code"] == "payload_too_large"


async def test_bodies_within_the_limit_pass_untouched() -> None:
    app = _Recorder()
    headers = [(b"content-length", b"not-a-number")]  # unusable: counted while read instead
    sent = await run(BodyLimitMiddleware(app, max_bytes=10), headers, [b"12345", b"67890"])
    assert app.body == b"1234567890"
    assert sent[0]["status"] == 204


async def test_streamed_bodies_are_cut_at_the_limit() -> None:
    app = _Recorder()
    with pytest.raises(HTTPException) as refused:
        await run(BodyLimitMiddleware(app, max_bytes=10), [], [b"123456", b"789012", b"x"])
    assert refused.value.status_code == 413
    assert app.body == b"123456"


async def test_websockets_are_passed_through() -> None:
    app = _Recorder()

    async def nothing() -> Message:
        raise AssertionError("not called")

    async def ignore(message: Message) -> None:
        raise AssertionError("not called")

    await BodyLimitMiddleware(app, max_bytes=1)({"type": "websocket"}, nothing, ignore)
    assert app.called
