"""Process-wide resources of one API replica, created in the application lifespan."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import Request, WebSocket
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from perimeter.api.security import RevocationList, TokenService
from perimeter.bus.relay import OutboxRelay
from perimeter.config import Settings
from perimeter.ops.looplag import LoopLagMonitor


@dataclass(slots=True)
class AppState:
    settings: Settings
    instance: str
    db: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    nc: NatsClient
    js: JetStreamContext
    relay: OutboxRelay
    tokens: TokenService
    revoked: RevocationList
    looplag: LoopLagMonitor
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def spawn(self, coro: object, name: str) -> None:
        """Run a background coroutine for the lifetime of the process."""
        self.tasks.append(asyncio.create_task(coro, name=name))  # type: ignore[arg-type]


def state_of(connection: Request | WebSocket) -> AppState:
    state: AppState = connection.app.state.perimeter
    return state
