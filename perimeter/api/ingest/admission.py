"""Admission control for device ingest: stop accepting work the pipeline cannot absorb.

The engine consumes TELEMETRY through one durable consumer per partition. What those consumers
have not finished — messages not yet delivered plus messages delivered but not yet acknowledged —
is the pipeline's backlog. Admitting reports faster than the engine drains them only grows it:
latency climbs, memory fills, and eventually every alert is late. So each API replica samples the
backlog a few times a second and stops admitting reports when it reaches a high watermark, until it
falls back to a low watermark; the gap between the two keeps the state from flapping around a
single threshold.

While shedding, HTTP ingest answers 503 with ``Retry-After`` before it reads the request body, and
WebSocket ingest stops granting credit. The retry hint is the time the engine needs to drain the
excess at its measured rate. A backlog that cannot be measured (broker unreachable, consumer
missing) counts as too large: the controller fails closed.

:class:`Admission` is the decision logic, pure and clock-free; :class:`AdmissionController` feeds it
samples from the broker and publishes the outcome as metrics.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from nats.js import JetStreamContext
from nats.js.api import ConsumerInfo
from prometheus_client import Counter, Gauge

from perimeter.config import IngestSettings
from perimeter.domain.clock import SYSTEM_CLOCK, Clock
from perimeter.wire import subjects

log = structlog.get_logger(__name__)

RETRY_AFTER_MIN_S = 1
RETRY_AFTER_MAX_S = 30
RETRY_AFTER_UNKNOWN_S = 5
CONSUMER_LIST_PAGE = 256  # JetStream's page size for consumer listings

ADMITTING = Gauge("perimeter_ingest_admitting", "1 while ingest is admitted, 0 while shedding")
BACKLOG = Gauge(
    "perimeter_ingest_backlog", "Engine backlog (pending + unacknowledged); NaN when unknown"
)
DRAIN_RATE = Gauge("perimeter_ingest_drain_rate", "Reports per second the engine acknowledges")
SAMPLE_FAILURES = Counter(
    "perimeter_ingest_admission_sample_failures_total", "Backlog samples that could not be taken"
)
TRANSITIONS = Counter(
    "perimeter_ingest_admission_transitions_total", "Admission state changes", ["state"]
)


class AdmissionState(StrEnum):
    OPEN = "open"
    SHEDDING = "shedding"


@dataclass(frozen=True, slots=True)
class Backlog:
    """One sample of the engine consumers."""

    pending: int
    acked: Mapping[str, int]
    at: float


class Admission:
    """Watermarks with hysteresis, a smoothed drain rate and the retry hint derived from both."""

    def __init__(self, *, high: int, low: int, smoothing: float = 0.5) -> None:
        if not 0 <= low < high:
            msg = f"watermarks must satisfy 0 <= low < high, got low={low} high={high}"
            raise ValueError(msg)
        if not 0 < smoothing <= 1:
            msg = "smoothing must be in (0, 1]"
            raise ValueError(msg)
        self.high = high
        self.low = low
        self._smoothing = smoothing
        self.state = AdmissionState.SHEDDING  # nothing measured yet: fail closed
        self.backlog: int | None = None
        self.drain_rate: float | None = None
        self._previous: Backlog | None = None

    @property
    def admitting(self) -> bool:
        return self.state is AdmissionState.OPEN

    def observe(self, sample: Backlog | None) -> bool:
        """Take a sample (``None``: could not measure); ``True`` when the state changed."""
        before = self.state
        if sample is None:
            self.backlog = None
            self._previous = None
            self.state = AdmissionState.SHEDDING
            return self.state is not before
        self._update_drain_rate(sample)
        self.backlog = sample.pending
        if self.state is AdmissionState.OPEN and sample.pending >= self.high:
            self.state = AdmissionState.SHEDDING
        elif self.state is AdmissionState.SHEDDING and sample.pending <= self.low:
            self.state = AdmissionState.OPEN
        return self.state is not before

    def _update_drain_rate(self, sample: Backlog) -> None:
        previous, self._previous = self._previous, sample
        if previous is None or sample.at <= previous.at:
            return
        # Per consumer, so a consumer recreated from scratch (sequence reset) counts as zero
        # progress instead of a huge negative one.
        drained = sum(
            max(0, acked - previous.acked.get(name, acked)) for name, acked in sample.acked.items()
        )
        instant = drained / (sample.at - previous.at)
        if self.drain_rate is None:
            self.drain_rate = instant
        else:
            self.drain_rate += self._smoothing * (instant - self.drain_rate)

    def retry_after_s(self) -> int:
        """Seconds a shed client should wait: the time to drain back to the low watermark."""
        if self.backlog is None or self.drain_rate is None:
            return RETRY_AFTER_UNKNOWN_S
        if self.drain_rate <= 0:
            return RETRY_AFTER_MAX_S
        seconds = math.ceil(max(0, self.backlog - self.low) / self.drain_rate)
        return max(RETRY_AFTER_MIN_S, min(RETRY_AFTER_MAX_S, seconds))


class AdmissionController:
    """Samples the engine backlog every ``INGEST_ADMISSION_SAMPLE_MS`` and keeps the verdict."""

    def __init__(
        self,
        js: JetStreamContext,
        *,
        settings: IngestSettings,
        partitions: int,
        sample_timeout_s: float = 2.0,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._js = js
        self._consumers = frozenset(subjects.engine_consumer(p) for p in range(partitions))
        self._interval_s = settings.admission_sample_ms / 1000
        self._sample_timeout_s = sample_timeout_s
        self._clock = clock
        self.admission = Admission(high=settings.admission_high, low=settings.admission_low)
        self._publish_metrics()

    @property
    def admitting(self) -> bool:
        return self.admission.admitting

    def retry_after_s(self) -> int:
        return self.admission.retry_after_s()

    async def sample(self) -> Backlog | None:
        """The engine backlog now, or ``None`` when it cannot be measured."""
        try:
            async with asyncio.timeout(self._sample_timeout_s):
                infos = await self._engine_consumers()
        except Exception as exc:  # any failure to measure means "unknown", i.e. shed
            SAMPLE_FAILURES.inc()
            log.warning("ingest.backlog_unknown", error=repr(exc))
            return None
        missing = self._consumers - infos.keys()
        if missing:
            SAMPLE_FAILURES.inc()
            log.warning("ingest.backlog_unknown", missing_consumers=sorted(missing))
            return None
        return Backlog(
            pending=sum(
                (info.num_pending or 0) + (info.num_ack_pending or 0) for info in infos.values()
            ),
            acked={
                name: info.ack_floor.consumer_seq if info.ack_floor else 0
                for name, info in infos.items()
            },
            at=self._clock.monotonic(),
        )

    async def _engine_consumers(self) -> dict[str, ConsumerInfo]:
        found: dict[str, ConsumerInfo] = {}
        offset = 0
        while True:
            page = await self._js.consumers_info(subjects.TELEMETRY_STREAM, offset=offset)
            found.update((info.name, info) for info in page if info.name in self._consumers)
            if len(page) < CONSUMER_LIST_PAGE or len(found) == len(self._consumers):
                return found
            offset += len(page)

    async def sample_once(self) -> None:
        if self.admission.observe(await self.sample()):
            state = self.admission.state
            TRANSITIONS.labels(state.value).inc()
            if state is AdmissionState.SHEDDING:
                log.warning(
                    "ingest.shedding", backlog=self.admission.backlog, high=self.admission.high
                )
            else:
                log.info("ingest.admitting", backlog=self.admission.backlog, low=self.admission.low)
        self._publish_metrics()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ingest.admission_sample_crashed")
            try:
                await asyncio.wait_for(stop.wait(), self._interval_s)
            except TimeoutError:
                continue

    def _publish_metrics(self) -> None:
        admission = self.admission
        ADMITTING.set(1 if admission.admitting else 0)
        BACKLOG.set(math.nan if admission.backlog is None else admission.backlog)
        DRAIN_RATE.set(admission.drain_rate or 0.0)

    def snapshot(self) -> dict[str, Any]:
        admission = self.admission
        return {
            "admission": admission.state.value,
            "lag": admission.backlog,
            "drain_rate": round(admission.drain_rate, 1) if admission.drain_rate else 0.0,
        }
