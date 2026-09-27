"""A device's recent track, read back from the TELEMETRY stream with batched direct gets.

TELEMETRY keeps every accepted report for its retention period, so it doubles as the trail log and
no history table is needed. Reading it is stateless on the broker: one batched direct get
(``$JS.API.DIRECT.GET.TELEMETRY`` with ``next_by_subj`` = ``tlm.*.<device>``, whatever the
partition) answers with up to ``batch`` matching messages from a start time or sequence, each with
its stream sequence, then an end-of-batch marker (status 204) that says how many more match; no
match at all is a single 404. No consumer is created: nothing is left behind when a read is
abandoned, and the api's broker account needs no right to create, pull from or delete consumers on
TELEMETRY, which keeps the engine's durable consumers out of its reach.

Every read is bounded by points, messages scanned, bytes per batch and time: a slow broker or an
unusually chatty device can make a trail shorter (reported as incomplete), never make a request
hang. When the window holds more reports than the point cap, the read starts later, in
proportion, so a device reporting at a steady rate gets the most recent part of its track rather
than the oldest; a burst too dense to thin out that way is cut by the scan budget, keeping the
newest points it read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import msgspec
import nats.errors
import structlog
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from perimeter.domain.reports import ms_to_datetime
from perimeter.wire import subjects, telemetry

log = structlog.get_logger(__name__)

DIRECT_GET = f"$JS.API.DIRECT.GET.{subjects.TELEMETRY_STREAM}"
BATCH_SIZE = 500
BATCH_MAX_BYTES = 512 * 1024
SCAN_FACTOR = 2
END_OF_BATCH = "204"
NOT_FOUND = "404"

_encoder = msgspec.json.Encoder()


@dataclass(frozen=True, slots=True)
class TrailPoint:
    recorded_at_ms: int
    lat: float
    lon: float
    speed: float | None
    heading: float | None


@dataclass(frozen=True, slots=True)
class Trail:
    device_id: str
    since_ms: int
    points: list[TrailPoint]
    complete: bool


class TrailReadError(Exception):
    """The broker answered a direct get with something other than messages, 204 or 404."""


@dataclass(slots=True)
class _Batch:
    points: list[TrailPoint]
    delivered: int
    last_seq: int
    pending: int


class _DirectReader:
    """Batched direct gets for one device, one after another, on a private inbox."""

    def __init__(self, nc: NatsClient, device_id: str) -> None:
        self._nc = nc
        self._filter = subjects.telemetry_of_device(device_id)
        self._inbox = nc.new_inbox()
        self._subscription: Subscription | None = None

    async def __aenter__(self) -> _DirectReader:
        self._subscription = await self._nc.subscribe(self._inbox)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._subscription is not None:
            await self._subscription.unsubscribe()

    async def batch(self, start: dict[str, Any], size: int) -> _Batch:
        """Up to ``size`` reports from ``start`` (``start_time`` or ``seq``) and what is left."""
        assert self._subscription is not None
        request = {
            **start,
            "next_by_subj": self._filter,
            "batch": size,
            "max_bytes": BATCH_MAX_BYTES,
        }
        await self._nc.publish(DIRECT_GET, _encoder.encode(request), reply=self._inbox)
        batch = _Batch(points=[], delivered=0, last_seq=0, pending=0)
        while True:
            message: Msg = await self._subscription.next_msg(timeout=None)
            headers = message.headers or {}
            status = headers.get("Status")
            if status is None:
                batch.delivered += 1
                batch.last_seq = int(headers["Nats-Sequence"])
                _append(batch.points, message.data)
            elif status == END_OF_BATCH:
                batch.last_seq = int(headers.get("Nats-Last-Sequence", batch.last_seq))
                batch.pending = int(headers.get("Nats-Num-Pending", 0))
                return batch
            elif status == NOT_FOUND:
                return batch
            else:
                msg = f"direct get answered {status} {headers.get('Description', '')}".strip()
                raise TrailReadError(msg)


def _append(points: list[TrailPoint], data: bytes) -> None:
    try:
        record = telemetry.decode(data)
    except telemetry.DecodeError:  # not a report: skip it, the rest of the trail is still good
        return
    points.append(
        TrailPoint(record.recorded_at_ms, record.lat, record.lon, record.speed, record.heading)
    )


def _from_time(ms: int) -> dict[str, Any]:
    return {"start_time": ms_to_datetime(ms).isoformat()}


async def _drain(
    reader: _DirectReader, start_ms: int, *, budget: int, batch_size: int, into: list[TrailPoint]
) -> bool:
    """Read from ``start_ms`` until caught up (``True``) or ``budget`` messages were scanned."""
    start = _from_time(start_ms)
    scanned = 0
    while scanned < budget:
        batch = await reader.batch(start, min(batch_size, budget - scanned))
        scanned += batch.delivered
        into.extend(batch.points)
        if batch.pending == 0:
            return True
        if batch.delivered == 0:  # nothing fits the byte cap: give up rather than spin
            return False
        start = {"seq": batch.last_seq + 1}
    return False


async def read_trail(
    nc: NatsClient,
    device_id: str,
    *,
    since_ms: int,
    now_ms: int,
    max_points: int,
    timeout_s: float,
    batch_size: int = BATCH_SIZE,
) -> Trail:
    """Reports of ``device_id`` recorded since ``since_ms``, oldest first, newest ``max_points``."""
    points: list[TrailPoint] = []
    complete = False
    try:
        async with asyncio.timeout(timeout_s), _DirectReader(nc, device_id) as reader:
            probe = await reader.batch(_from_time(since_ms), 1)
            matching = probe.delivered + probe.pending
            start_ms = since_ms
            if matching > max_points and now_ms > since_ms:
                start_ms = now_ms - (now_ms - since_ms) * max_points // matching
            caught_up = matching == 0 or await _drain(
                reader,
                start_ms,
                budget=max_points * SCAN_FACTOR,
                batch_size=batch_size,
                into=points,
            )
            complete = caught_up and start_ms == since_ms
    except TimeoutError:
        log.info("trail.deadline", device_id=device_id, points=len(points), timeout_s=timeout_s)
    except (TrailReadError, nats.errors.Error) as exc:
        log.warning("trail.read_failed", device_id=device_id, error=str(exc))
    return _trail(device_id, since_ms, points, max_points=max_points, complete=complete)


def _trail(
    device_id: str, since_ms: int, points: list[TrailPoint], *, max_points: int, complete: bool
) -> Trail:
    """Event-time order, one point per timestamp, inside the window, at most ``max_points``."""
    by_time: dict[int, TrailPoint] = {}
    for point in points:
        if point.recorded_at_ms >= since_ms:
            by_time.setdefault(point.recorded_at_ms, point)
    ordered = [by_time[at] for at in sorted(by_time)]
    if len(ordered) > max_points:
        ordered = ordered[-max_points:]
        complete = False
    return Trail(device_id=device_id, since_ms=since_ms, points=ordered, complete=complete)
