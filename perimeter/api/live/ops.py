"""The ops board: the whole pipeline's heartbeats, pushed once a second to sessions that ask.

Every process publishes a one-second JSON heartbeat on ``sys.metrics.<service>.<instance>``. The
board keeps the latest one per instance, forgets instances silent for more than five seconds (a
stopped engine disappears from the view on its own) and sends
``{"type":"ops","services":[...],"ts":...}`` to the sessions that sent ``{"type":"ops","on":true}``.
Heartbeats are validated once and then forwarded as the bytes they arrived as.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

import msgspec
import structlog
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from nats.errors import Error as NatsError

from perimeter.wire import subjects

log = structlog.get_logger(__name__)

INTERVAL_S = 1.0
SILENCE_S = 5.0


class OpsViewer(Protocol):
    def offer_text(self, frame: bytes) -> bool: ...


class OpsBoard:
    def __init__(
        self,
        nc: NatsClient,
        *,
        interval_s: float = INTERVAL_S,
        silence_s: float = SILENCE_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._nc = nc
        self._interval_s = interval_s
        self._silence_s = silence_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._latest: dict[str, tuple[float, bytes]] = {}  # subject -> (received at, payload)
        self._viewers: set[OpsViewer] = set()
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = await self._nc.subscribe(subjects.METRICS_ALL, cb=self._on_heartbeat)

    async def _on_heartbeat(self, msg: Msg) -> None:
        self.record(msg.subject, msg.data)

    def record(self, subject: str, payload: bytes) -> None:
        """Keep ``payload`` as the latest heartbeat of ``subject`` if it is a JSON object."""
        try:
            decoded = msgspec.json.decode(payload)
        except msgspec.DecodeError:
            log.warning("live.invalid_heartbeat", subject=subject)
            return
        if isinstance(decoded, dict):
            self._latest[subject] = (self._clock(), payload)

    def watch(self, viewer: OpsViewer) -> None:
        if viewer not in self._viewers:
            self._viewers.add(viewer)
            viewer.offer_text(self.frame())

    def unwatch(self, viewer: OpsViewer) -> None:
        self._viewers.discard(viewer)

    def frame(self) -> bytes:
        cutoff = self._clock() - self._silence_s
        for subject in [s for s, (at, _) in self._latest.items() if at < cutoff]:
            del self._latest[subject]
        services = b",".join(payload for _, (_, payload) in sorted(self._latest.items()))
        ts = b"%.3f" % self._wall_clock()
        return b'{"type":"ops","services":[' + services + b'],"ts":' + ts + b"}"

    def snapshot(self) -> dict[str, int]:
        return {"ops_viewers": len(self._viewers)}

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._interval_s)
            if self._viewers and not stop.is_set():
                frame = self.frame()
                for viewer in list(self._viewers):
                    viewer.offer_text(frame)

    async def close(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            with suppress(NatsError):
                await subscription.unsubscribe()
        self._viewers.clear()
