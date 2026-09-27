"""Durable publishing of validated reports into the TELEMETRY stream.

A report is accepted only once JetStream has stored it: every record is published with
``publish_async`` (one round trip per batch rather than per report), and the caller gets an answer
only after every acknowledgement arrived. Each message carries ``Nats-Msg-Id`` =
``<device>:<recorded_at_ms>``, so a device that retries a report it never saw acknowledged is
stored once within the stream's de-duplication window.

Memory is bounded by an in-flight budget of reports awaiting acknowledgement, shared by every
request and socket of the replica (``INGEST_MAX_INFLIGHT``). A caller that cannot get budget
within a short wait is told to back off (HTTP 429) instead of queueing without limit. The budget
is weighted and first-come-first-served, so a large batch cannot be starved by a stream of small
ones.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import structlog
from nats.aio.client import Client as NatsClient
from nats.js.api import PubAck
from prometheus_client import Counter, Gauge, Histogram

from perimeter.config import IngestSettings
from perimeter.domain.clock import SYSTEM_CLOCK, Clock
from perimeter.domain.reports import TelemetryRecord
from perimeter.ops import tracing
from perimeter.wire import subjects, telemetry

log = structlog.get_logger(__name__)

ACCEPTED = Counter("perimeter_ingest_accepted_total", "Reports stored in TELEMETRY")
DUPLICATES = Counter(
    "perimeter_ingest_duplicates_total", "Accepted reports JetStream had already stored"
)
REJECTED = Counter("perimeter_ingest_rejected_total", "Reports rejected by validation", ["code"])
PUBLISH_FAILURES = Counter(
    "perimeter_ingest_publish_failures_total", "Batches JetStream did not fully acknowledge"
)
OVERLOADED = Counter(
    "perimeter_ingest_overloaded_total", "Batches refused because the in-flight budget was full"
)
INFLIGHT = Gauge("perimeter_ingest_inflight", "Reports published and awaiting acknowledgement")
ACK_SECONDS = Histogram(
    "perimeter_ingest_ack_seconds",
    "Time from the first publish of a batch to its last acknowledgement",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


class IngestOverloaded(Exception):  # noqa: N818 - a condition the caller reports, not a bug
    """No in-flight budget became free in time; the client should retry shortly."""


class IngestUnavailable(Exception):  # noqa: N818 - a condition the caller reports, not a bug
    """JetStream did not acknowledge the batch; nothing about it may be assumed stored."""


class InflightBudget:
    """A weighted, first-come-first-served semaphore counting reports rather than callers."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            msg = "capacity must be at least 1"
            raise ValueError(msg)
        self._capacity = capacity
        self._available = capacity
        self._waiters: deque[tuple[int, asyncio.Future[None]]] = deque()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        return self._capacity - self._available

    async def acquire(self, amount: int, *, wait_s: float) -> bool:
        """Take ``amount`` units, waiting at most ``wait_s`` seconds; ``False`` if that failed."""
        if amount > self._capacity:
            msg = f"cannot acquire {amount} units from a budget of {self._capacity}"
            raise ValueError(msg)
        if not self._waiters and self._available >= amount:
            self._available -= amount
            return True
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (amount, waiter)
        self._waiters.append(entry)
        try:
            async with asyncio.timeout(wait_s):
                await waiter
        except BaseException as exc:
            if waiter.done() and not waiter.cancelled():
                self.release(amount)  # granted in the same instant the wait gave up
            else:
                waiter.cancel()
                with suppress(ValueError):
                    self._waiters.remove(entry)
                self._grant()  # a large request leaving the head may unblock smaller ones
            if isinstance(exc, TimeoutError):
                return False
            raise
        return True

    def release(self, amount: int) -> None:
        self._available = min(self._capacity, self._available + amount)
        self._grant()

    def _grant(self) -> None:
        while self._waiters and self._waiters[0][0] <= self._available:
            amount, waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._available -= amount
            waiter.set_result(None)


class RateCounter:
    """Events per second over the last complete second (for the one-second heartbeat)."""

    SLOTS = 4

    def __init__(self, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock
        self._seconds = [-1] * self.SLOTS
        self._counts = [0] * self.SLOTS

    def add(self, amount: int = 1) -> None:
        second = int(self._clock.monotonic())
        slot = second % self.SLOTS
        if self._seconds[slot] != second:
            self._seconds[slot] = second
            self._counts[slot] = 0
        self._counts[slot] += amount

    def rate(self) -> float:
        previous = int(self._clock.monotonic()) - 1
        slot = previous % self.SLOTS
        return float(self._counts[slot]) if self._seconds[slot] == previous else 0.0


class LatencyWindow:
    """Recent latencies (bounded) for a percentile in the heartbeat."""

    def __init__(self, *, horizon_s: float = 5.0, size: int = 4_096, clock: Clock = SYSTEM_CLOCK):
        self._horizon_s = horizon_s
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque(maxlen=size)

    def add(self, seconds: float) -> None:
        self._samples.append((self._clock.monotonic(), seconds))

    def percentile(self, q: float) -> float | None:
        cutoff = self._clock.monotonic() - self._horizon_s
        recent = sorted(value for at, value in self._samples if at >= cutoff)
        if not recent:
            return None
        return recent[min(len(recent) - 1, round(q * (len(recent) - 1)))]


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    accepted: int
    duplicates: int


class TelemetryPublisher:
    def __init__(
        self,
        nc: NatsClient,
        *,
        settings: IngestSettings,
        ack_timeout_s: float,
        budget_wait_s: float = 0.5,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        # A private JetStream context: its publish_async window must never be the bottleneck
        # below our own budget (the client blocks without a timeout when that window is full).
        self._js = nc.jetstream(publish_async_max_pending=settings.max_inflight)
        self._budget = InflightBudget(settings.max_inflight)
        self._ack_timeout_s = ack_timeout_s
        self._budget_wait_s = budget_wait_s
        self._accepted = RateCounter(clock)
        self._rejected = RateCounter(clock)
        self._latency = LatencyWindow(clock=clock)

    @property
    def budget(self) -> InflightBudget:
        return self._budget

    async def publish(self, records: Sequence[TelemetryRecord]) -> PublishOutcome:
        """Store ``records`` durably; returns only after JetStream acknowledged all of them.

        Raises :class:`IngestOverloaded` when the in-flight budget stays full and
        :class:`IngestUnavailable` when any acknowledgement fails or does not arrive in time.
        """
        if not records:
            return PublishOutcome(0, 0)
        count = len(records)
        if not await self._budget.acquire(count, wait_s=self._budget_wait_s):
            OVERLOADED.inc()
            raise IngestOverloaded
        INFLIGHT.inc(count)
        started = time.perf_counter()
        try:
            acks = await self._publish_all(records)
        finally:
            self._budget.release(count)
            INFLIGHT.dec(count)
        elapsed = time.perf_counter() - started
        ACK_SECONDS.observe(elapsed)
        self._latency.add(elapsed)
        duplicates = sum(1 for ack in acks if ack.duplicate)
        ACCEPTED.inc(count)
        DUPLICATES.inc(duplicates)
        self._accepted.add(count)
        return PublishOutcome(accepted=count, duplicates=duplicates)

    async def _publish_all(self, records: Sequence[TelemetryRecord]) -> list[PubAck]:
        futures: list[asyncio.Future[PubAck]] = []
        try:
            for record in records:
                headers = {"Nats-Msg-Id": telemetry.dedup_id(record)}
                tracing.inject(headers)
                futures.append(
                    await self._js.publish_async(
                        subjects.telemetry(record.device_id),
                        telemetry.encode(record),
                        headers=headers,
                    )
                )
            done, pending = await asyncio.wait(futures, timeout=self._ack_timeout_s)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
        for future in pending:
            future.cancel()  # releases the client's reply slot; the message may still be stored
        failures = [f for f in done if f.cancelled() or f.exception() is not None]
        if pending or failures:
            PUBLISH_FAILURES.inc()
            first = next((f.exception() for f in failures if not f.cancelled()), None)
            log.warning(
                "ingest.publish_failed",
                reports=len(records),
                unacknowledged=len(pending),
                failed=len(failures),
                error=repr(first) if first else "timeout",
            )
            raise IngestUnavailable from first
        return [future.result() for future in futures]

    def note_rejected(self, codes: Sequence[str]) -> None:
        """Count reports that failed validation (they never reach the broker)."""
        for code in codes:
            REJECTED.labels(code).inc()
        self._rejected.add(len(codes))

    def snapshot(self) -> dict[str, Any]:
        p99 = self._latency.percentile(0.99)
        return {
            "ingest_rate": self._accepted.rate(),
            "ingest_rejected_rate": self._rejected.rate(),
            "ingest_inflight": self._budget.in_use,
            "publish_p99_ms": round(p99 * 1000, 2) if p99 is not None else None,
        }
