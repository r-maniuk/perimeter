"""Two real API replicas served by uvicorn in-process, and a small client for the live channel.

The replicas run exactly as in production (same application factory, uvicorn's
``websockets-sansio`` implementation, no per-message compression) against the test PostGIS and
NATS, on free local ports. Tests talk to them with the ``websockets`` client library.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from perimeter.api.app import create_app
from perimeter.api.security import COOKIE_NAME, IssuedToken, TokenService
from perimeter.api.state import AppState
from perimeter.config import LiveSettings, Settings
from perimeter.wire.frames import TileFrame, decode_bundle, decode_tile
from tests.support import eventually

type Frame = dict[str, Any] | bytes


class InProcessServer(uvicorn.Server):
    """A uvicorn server that leaves the test runner's signal handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass
class Replica:
    name: str
    app: FastAPI
    server: InProcessServer
    task: asyncio.Task[None]
    port: int

    @property
    def state(self) -> AppState:
        state: AppState = self.app.state.perimeter
        return state

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ws(self, token: str | None = None, **params: object) -> str:
        query = {"token": token} if token else {}
        query.update({k: str(v) for k, v in params.items()})
        suffix = f"?{urlencode(query)}" if query else ""
        return f"ws://127.0.0.1:{self.port}/v1/live{suffix}"

    async def settle(self) -> None:
        """Position subscriptions match the viewports and the broker has seen them."""
        await self.state.hub.tiles.settled()
        await self.state.nc.flush()

    async def stop(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 30)


async def start_replica(settings: Settings, name: str, monkeypatch: pytest.MonkeyPatch) -> Replica:
    monkeypatch.setenv("HOSTNAME", name)  # the replica's instance id
    app = create_app(settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app,
        http="httptools",
        ws="websockets-sansio",
        ws_per_message_deflate=False,
        ws_max_size=1024 * 1024,
        lifespan="on",
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = InProcessServer(config)
    task = asyncio.create_task(server.serve(sockets=[listener]), name=f"replica-{name}")
    await eventually(lambda: server.started or task.done(), within=30)
    if task.done():
        msg = f"replica {name} did not start"
        raise RuntimeError(msg) from task.exception()
    return Replica(name, app, server, task, port)


@contextlib.asynccontextmanager
async def replicas(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, *names: str
) -> AsyncIterator[list[Replica]]:
    started: list[Replica] = []
    try:
        for name in names:
            started.append(await start_replica(settings, name, monkeypatch))  # noqa: PERF401
        yield started
    finally:
        for replica in reversed(started):
            await replica.stop()


def with_live(settings: Settings, **overrides: Any) -> Settings:
    """The same settings with some ``LIVE_*`` values changed."""
    live = LiveSettings(**{**settings.live.model_dump(), **overrides})
    return Settings(
        database=settings.database,
        nats=settings.nats,
        security=settings.security,
        telemetry=settings.telemetry,
        ingest=settings.ingest,
        engine=settings.engine,
        live=live,
        observability=settings.observability,
    )


async def create_user(db: AsyncEngine, username: str) -> uuid.UUID:
    async with db.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(
                text("INSERT INTO users (username) VALUES (:u) RETURNING id"), {"u": username}
            )
        ).scalar_one()
    return user_id


def sign_in(settings: Settings, user_id: uuid.UUID, username: str) -> IssuedToken:
    return TokenService(settings.security).issue(user_id, username)


class LiveClient:
    """A live-channel client that reads on demand, so a test can also stop reading."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.backlog: list[Frame] = []

    @classmethod
    async def open(
        cls,
        url: str,
        *,
        origin: str | None = None,
        cookie: str | None = None,
        max_queue: int = 16,
    ) -> LiveClient:
        headers = {"Cookie": f"{COOKIE_NAME}={cookie}"} if cookie else None
        ws = await connect(
            url,
            origin=origin,  # type: ignore[arg-type]
            additional_headers=headers,
            proxy=None,
            max_size=None,
            max_queue=max_queue,
            open_timeout=10,
            compression=None,
        )
        return cls(ws)

    async def send(self, **message: Any) -> None:
        await self.ws.send(json.dumps(message))

    async def viewport(self, west: float, south: float, east: float, north: float) -> None:
        await self.send(type="viewport", bbox=[west, south, east, north])

    async def _read(self, within: float) -> Frame | None:
        try:
            message = await asyncio.wait_for(self.ws.recv(), within)
        except TimeoutError:
            return None
        return json.loads(message) if isinstance(message, str) else message

    async def _take[T](self, pick: Callable[[Frame], T | None], within: float, what: str) -> T:
        """Remove and return the first backlog item ``pick`` accepts, reading more as needed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while True:
            for index, item in enumerate(self.backlog):
                picked = pick(item)
                if picked is not None:
                    del self.backlog[index]
                    return picked
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = f"no {what} within {within}s; backlog: {self.describe()}"
                raise AssertionError(msg)
            received = await self._read(remaining)
            if received is not None:
                self.backlog.append(received)

    async def expect(
        self,
        kind: str,
        *,
        within: float = 5.0,
        where: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """The next text frame of type ``kind`` (other frames stay in the backlog)."""

        def pick(item: Frame) -> dict[str, Any] | None:
            if not isinstance(item, dict) or item.get("type") != kind:
                return None
            return item if where is None or where(item) else None

        return await self._take(pick, within, f"{kind!r} frame")

    async def bundle(self, *, within: float = 5.0) -> list[bytes]:
        """The tile frames of the next binary message, as the bytes that were sent."""

        def pick(item: Frame) -> list[bytes] | None:
            return [bytes(f) for f in decode_bundle(item)] if isinstance(item, bytes) else None

        return await self._take(pick, within, "position bundle")

    async def tiles(self, *, within: float = 5.0) -> list[TileFrame]:
        """The decoded tile frames of the next binary message."""
        return [decode_tile(frame) for frame in await self.bundle(within=within)]

    async def events(self, count: int, *, within: float = 5.0) -> list[dict[str, Any]]:
        return [await self.expect("event", within=within) for _ in range(count)]

    async def quiet(self, seconds: float) -> list[Frame]:
        """Everything that arrives within ``seconds`` (plus the backlog), consumed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while (remaining := deadline - loop.time()) > 0:
            received = await self._read(remaining)
            if received is not None:
                self.backlog.append(received)
        drained, self.backlog = self.backlog, []
        return drained

    async def closed(self, *, within: float = 5.0) -> int | None:
        """Read until the server closes the connection; the close code it sent."""
        try:
            async with asyncio.timeout(within):
                while True:
                    item = await self.ws.recv()
                    self.backlog.append(json.loads(item) if isinstance(item, str) else item)
        except ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd is not None else None

    async def close(self) -> None:
        await self.ws.close()

    def describe(self) -> list[str]:
        return [str(i.get("type")) if isinstance(i, dict) else "<binary>" for i in self.backlog]


def of_type(items: list[Frame], kind: str) -> list[dict[str, Any]]:
    """The text frames of type ``kind`` among ``items``."""
    return [item for item in items if isinstance(item, dict) and item.get("type") == kind]


@contextlib.asynccontextmanager
async def live_client(url: str, **options: Any) -> AsyncIterator[LiveClient]:
    client = await LiveClient.open(url, **options)
    try:
        yield client
    finally:
        await client.close()
