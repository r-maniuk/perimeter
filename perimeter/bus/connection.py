"""NATS connection with authentication, unbounded reconnects and logged state changes."""

from __future__ import annotations

import asyncio
from typing import Any

import nats
import structlog
from nats.aio.client import Client as NatsClient
from nats.errors import NoServersError

from perimeter.config import NatsSettings

log = structlog.get_logger(__name__)

PENDING_BYTES = 64 * 1024 * 1024


async def connect(settings: NatsSettings, *, name: str, attempts: int = 30) -> NatsClient:
    """Connect, retrying the *initial* connection with backoff (later drops reconnect forever)."""

    async def error_cb(exc: Exception) -> None:
        log.warning("nats.error", error=str(exc), error_type=type(exc).__name__)

    async def disconnected_cb() -> None:
        log.warning("nats.disconnected")

    async def reconnected_cb() -> None:
        log.info("nats.reconnected")

    async def closed_cb() -> None:
        log.info("nats.closed")

    options: dict[str, Any] = {
        "servers": [settings.url],
        "name": name,
        "connect_timeout": settings.connect_timeout_s,
        "allow_reconnect": True,
        "max_reconnect_attempts": -1,
        "reconnect_time_wait": 0.5,
        "ping_interval": 10,
        "max_outstanding_pings": 3,
        "pending_size": PENDING_BYTES,
        "error_cb": error_cb,
        "disconnected_cb": disconnected_cb,
        "reconnected_cb": reconnected_cb,
        "closed_cb": closed_cb,
    }
    if settings.user:
        options["user"] = settings.user
        options["password"] = settings.password.get_secret_value() if settings.password else ""

    delay = 0.25
    for attempt in range(1, attempts + 1):
        try:
            return await nats.connect(**options)
        except (NoServersError, OSError, TimeoutError) as exc:
            if attempt == attempts:
                raise
            log.info("nats.connect_retry", attempt=attempt, error=str(exc), retry_in_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
    raise AssertionError("unreachable")  # pragma: no cover
