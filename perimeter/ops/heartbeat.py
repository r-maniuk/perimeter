"""One-second metric heartbeats on NATS, aggregated by the API for the dashboard's ops view.

Prometheus remains the source of truth for monitoring; the heartbeat exists so the demo UI can show
the whole pipeline live (ingest rate, partition lag, batch latency, sessions) without requiring a
monitoring stack to be running.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Callable
from typing import Any

import msgspec
import structlog
from nats.aio.client import Client as NatsClient

from perimeter.wire import subjects

log = structlog.get_logger(__name__)

_encoder = msgspec.json.Encoder()


def instance_id(explicit: str | None = None) -> str:
    """Stable, subject-safe identifier of this process (container hostname by default)."""
    raw = explicit or os.environ.get("HOSTNAME") or socket.gethostname()
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw)
    return cleaned[:48] or "unknown"


class Heartbeat:
    def __init__(
        self,
        nc: NatsClient,
        *,
        service: str,
        instance: str,
        snapshot: Callable[[], dict[str, Any]],
        interval_s: float = 1.0,
    ) -> None:
        self._nc = nc
        self._subject = subjects.metrics_heartbeat(service, instance)
        self._service = service
        self._instance = instance
        self._snapshot = snapshot
        self._interval_s = interval_s

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                payload = {
                    "service": self._service,
                    "instance": self._instance,
                    "ts": time.time(),
                    **self._snapshot(),
                }
                await self._nc.publish(self._subject, _encoder.encode(payload))
            except Exception:
                log.debug("heartbeat.publish_failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), self._interval_s)
            except TimeoutError:
                continue
