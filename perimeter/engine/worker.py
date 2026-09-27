"""The consumer of one telemetry partition.

A worker consumes its partition only under the partition's lease, and acts on that in three
places: it stops fetching as soon as the lease is no longer known to be valid, every batch it
applies is fenced with the lease's *current* token (a paused worker that lost the lease cannot
commit), and when it stops it hands messages it has not applied straight back to the broker.

Taking a partition over starts with recovery. Messages the previous owner was given but never
acknowledged (it crashed, or froze past its lease) come back from the broker only when their ack
wait runs out, long after the new owner has applied newer reports of the same devices, and would
then be discarded as late, losing the transitions they carry. So the new owner first reads that
range from the stream (between the consumer's acknowledgement floor and the last message it
delivered) and applies it, then starts fetching. When the broker redelivers those messages later
they are late duplicates: acknowledged, and ignored.

Messages are settled only after the batch they belong to has committed:

* applied: ``ack``;
* undecodable or invalid, so they can never be applied: ``term``, counted as poison;
* any failure, including being fenced out: ``nak`` without delay, and for a failure a pause before
  the next fetch. The broker serves redeliveries before new messages, so the retried reports are
  applied before any that arrived after them, whether by this worker after its pause or by a new
  owner at once. An unexpected error is retried too rather than dropping the batch: validation has
  already rejected every record the schema cannot hold, so what remains points at the system
  (database, schema, code), and dropping data would only hide it.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from enum import Enum, StrEnum
from typing import Literal, NamedTuple, Protocol

import structlog
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.errors import NotFoundError

from perimeter.bus.leases import FencingToken, Lease
from perimeter.domain.clock import SYSTEM_CLOCK, Clock
from perimeter.domain.reports import TelemetryRecord
from perimeter.engine.batch import FencedOut, is_transient
from perimeter.engine.metrics import BATCHES, POISON, RECOVERED
from perimeter.ops import tracing
from perimeter.wire import subjects, telemetry

log = structlog.get_logger(__name__)

LEASE_POLL_S = 0.1
DRAIN_FETCH_TIMEOUT_S = 0.1

_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_TIMESTAMP_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z, the end of timestamptz input

InFlight = Callable[[], Awaitable[Sequence[tuple[str, bytes]]]]
"""Reads ``(subject, data)`` of the partition's delivered but unacknowledged messages."""


class WorkerExit(StrEnum):
    STOPPED = "stopped"
    FENCED = "fenced"


class LeaseHandle:
    """The lease a worker runs under, kept current by the coordinator.

    ``valid_until`` is a monotonic deadline set a safety margin before the broker could expire the
    lease; past it the worker stops fetching until the next successful renewal.
    """

    __slots__ = ("_lease", "_valid_until")

    def __init__(self, lease: Lease, *, valid_until: float) -> None:
        self._lease = lease
        self._valid_until = valid_until

    @property
    def lease(self) -> Lease:
        return self._lease

    @property
    def token(self) -> FencingToken:
        return self._lease.token

    def valid(self, now: float) -> bool:
        return now < self._valid_until

    def refresh(self, lease: Lease, *, valid_until: float) -> None:
        self._lease = lease
        self._valid_until = valid_until

    def revoke(self) -> None:
        self._valid_until = -math.inf


class Backoff:
    """Exponential retry delays: ``initial``, doubling, capped; reset after a success."""

    def __init__(self, initial_s: float = 1.0, cap_s: float = 15.0) -> None:
        self._initial_s = initial_s
        self._cap_s = cap_s
        self._next_s = initial_s

    def next(self) -> float:
        delay = self._next_s
        self._next_s = min(self._next_s * 2, self._cap_s)
        return delay

    def reset(self) -> None:
        self._next_s = self._initial_s


class PullSubscription(Protocol):
    @property
    def pending_msgs(self) -> int: ...

    async def fetch(
        self,
        batch: int = ...,
        timeout: float | None = ...,  # noqa: ASYNC109 - nats-py's own signature
    ) -> list[Msg]: ...

    async def unsubscribe(self) -> None: ...


class BatchApplier(Protocol):
    async def apply(
        self,
        partition: int,
        token: FencingToken,
        records: Sequence[TelemetryRecord],
        *,
        trace: Mapping[str, str] | None = None,
    ) -> object: ...


class Rejected(NamedTuple):
    reason: str
    detail: str


def problem_of(record: TelemetryRecord, subject: str) -> str | None:
    """Why ``record`` (stored under ``subject``) can never be applied, or ``None`` if it can.

    Mirrors the ingest schema, so it only rejects what reached the stream without going
    through the api. The device check also guards partitioning: a record filed under another
    device's subject would be applied by a partition that does not own its device.
    """
    # Comparisons are written so that NaN fails them: NaN is never inside a range.
    checks = (
        (_DEVICE_ID.fullmatch(record.device_id) is not None, "device id is not a subject token"),
        (subjects.device_of(subject) == record.device_id, "device id does not match its subject"),
        (-90.0 <= record.lat <= 90.0, "latitude out of range"),
        (-180.0 <= record.lon <= 180.0, "longitude out of range"),
        (0 <= record.recorded_at_ms <= _MAX_TIMESTAMP_MS, "recorded_at out of range"),
        (0 <= record.received_at_ms <= _MAX_TIMESTAMP_MS, "received_at out of range"),
        (_within(record.speed, 1_000.0), "speed out of range"),
        (_within(record.heading, 360.0), "heading out of range"),
        (_within(record.accuracy, 100_000.0), "accuracy out of range"),
    )
    return next((problem for ok, problem in checks if not ok), None)


def _within(value: float | None, high: float) -> bool:
    return value is None or 0.0 <= value <= high


def parse(subject: str, data: bytes) -> TelemetryRecord | Rejected:
    """The record a message carries, or why it can never be applied."""
    try:
        record = telemetry.decode(data)
    except telemetry.DecodeError as exc:
        return Rejected("undecodable", str(exc))
    problem = problem_of(record, subject)
    return record if problem is None else Rejected("invalid", problem)


async def unacknowledged(js: JetStreamContext, partition: int) -> list[tuple[str, bytes]]:
    """Messages of ``partition`` its consumer delivered but nobody acknowledged, oldest first.

    They lie between the consumer's acknowledgement floor and the last message it delivered;
    the stream holds every partition, so each read asks for the next message of this one.
    """
    info = await js.consumer_info(subjects.TELEMETRY_STREAM, subjects.engine_consumer(partition))
    if not info.num_ack_pending or info.delivered is None:
        return []
    seq = (info.ack_floor.stream_seq if info.ack_floor is not None else 0) + 1
    last = info.delivered.stream_seq
    found: list[tuple[str, bytes]] = []
    while seq <= last:
        try:
            raw = await js.get_msg(
                subjects.TELEMETRY_STREAM,
                seq,
                subject=subjects.telemetry_partition(partition),
                direct=True,
                next=True,
            )
        except NotFoundError:  # nothing of this partition left in the range (or it aged out)
            break
        if raw.seq is None or raw.seq > last:
            break
        found.append((raw.subject or "", raw.data or b""))
        seq = raw.seq + 1
    return found


class _Outcome(Enum):
    DONE = "done"
    RETRY = "retry"
    FENCED = "fenced"
    STOPPED = "stopped"


class PartitionWorker:
    """Recover, then fetch, decode, apply, settle: one partition, one batch at a time."""

    def __init__(
        self,
        partition: int,
        *,
        subscribe: Callable[[], Awaitable[PullSubscription]],
        in_flight: InFlight,
        processor: BatchApplier,
        lease: LeaseHandle,
        batch_max: int,
        fetch_wait_s: float,
        linger_s: float = 0.0,
        clock: Clock = SYSTEM_CLOCK,
        backoff: Backoff | None = None,
    ) -> None:
        self._partition = partition
        self._subscribe = subscribe
        self._in_flight = in_flight
        self._processor = processor
        self._lease = lease
        self._batch_max = batch_max
        self._fetch_wait_s = fetch_wait_s
        self._linger_s = linger_s
        self._clock = clock
        self._backoff = backoff or Backoff()
        self._stopping = asyncio.Event()
        self._pause_s = 0.0
        self._log = log.bind(partition=partition)

    @property
    def partition(self) -> int:
        return self._partition

    def stop(self) -> None:
        """Finish the batch in hand, then return; never interrupts a fetch or a transaction."""
        self._stopping.set()

    async def run(self) -> WorkerExit:
        recovered = await self._recover()
        if recovered is not _Outcome.DONE:
            return WorkerExit.FENCED if recovered is _Outcome.FENCED else WorkerExit.STOPPED
        sub = await self._bind()
        if sub is None:
            return WorkerExit.STOPPED
        try:
            return await self._consume(sub)
        finally:
            await self._hand_back(sub)

    async def _recover(self) -> _Outcome:
        """Apply what the previous owner of the partition received but never acknowledged."""
        while not self._stopping.is_set():
            if not self._lease.valid(self._clock.monotonic()):
                await self._sleep(LEASE_POLL_S)
                continue
            try:
                pending = await self._in_flight()
            except Exception as exc:
                delay = self._backoff.next()
                self._log.warning("engine.recovery_read_failed", error=repr(exc), retry_in_s=delay)
                await self._sleep(delay)
                continue
            records = [r for s, d in pending if isinstance(r := parse(s, d), TelemetryRecord)]
            if records:
                self._log.info("engine.recovering", messages=len(pending), reports=len(records))
            for start in range(0, len(records), self._batch_max):
                outcome = await self._apply_until_done(records[start : start + self._batch_max])
                if outcome is not _Outcome.DONE:
                    return outcome
            RECOVERED.inc(len(records))
            return _Outcome.DONE
        return _Outcome.STOPPED

    async def _apply_until_done(self, records: Sequence[TelemetryRecord]) -> _Outcome:
        while not self._stopping.is_set():
            outcome = await self._apply(records)
            if outcome is not _Outcome.RETRY:
                return outcome
            await self._sleep(self._pause_s)
        return _Outcome.STOPPED

    async def _bind(self) -> PullSubscription | None:
        while not self._stopping.is_set():
            try:
                return await self._subscribe()
            except Exception as exc:
                delay = self._backoff.next()
                self._log.warning("engine.subscribe_failed", error=repr(exc), retry_in_s=delay)
                await self._sleep(delay)
        return None

    async def _consume(self, sub: PullSubscription) -> WorkerExit:
        full = True
        while not self._stopping.is_set():
            if not self._lease.valid(self._clock.monotonic()):
                await self._sleep(LEASE_POLL_S)
                continue
            if not full and self._linger_s:
                # The last fetch drained the partition: let reports gather for a moment, so one
                # transaction carries many of them. Under a backlog batches are full and nobody
                # waits; at idle the next fetch simply blocks until the first report arrives.
                await self._sleep(self._linger_s)
            try:
                msgs = await sub.fetch(self._batch_max, timeout=self._fetch_wait_s)
            except TimeoutError:  # nothing to do this time
                continue
            except Exception as exc:  # the broker is unreachable or the consumer went away
                delay = self._backoff.next()
                self._log.warning("engine.fetch_failed", error=repr(exc), retry_in_s=delay)
                await self._sleep(delay)
                continue
            full = len(msgs) >= self._batch_max
            outcome = await self._handle(msgs)
            if outcome is _Outcome.FENCED:
                return WorkerExit.FENCED
            if outcome is _Outcome.RETRY:
                await self._sleep(self._pause_s)
        return WorkerExit.STOPPED

    async def _handle(self, msgs: Sequence[Msg]) -> _Outcome:
        batch: list[tuple[Msg, TelemetryRecord]] = []
        for msg in msgs:
            parsed = parse(msg.subject, msg.data)
            if isinstance(parsed, Rejected):
                await self._reject(msg, parsed)
            else:
                batch.append((msg, parsed))
        if not batch:
            return _Outcome.DONE
        trace = _trace_context(msg for msg, _ in batch) if tracing.enabled() else None
        outcome = await self._apply([record for _, record in batch], trace=trace)
        await self._settle(batch, "ack" if outcome is _Outcome.DONE else "nak")
        return outcome

    async def _apply(
        self, records: Sequence[TelemetryRecord], *, trace: Mapping[str, str] | None = None
    ) -> _Outcome:
        try:
            await self._processor.apply(self._partition, self._lease.token, records, trace=trace)
        except FencedOut:
            BATCHES.labels("fenced").inc()
            self._log.warning("engine.fenced_out", token=self._lease.token, reports=len(records))
            return _Outcome.FENCED
        except Exception as exc:
            self._pause_s = self._backoff.next()
            if is_transient(exc):
                BATCHES.labels("retried").inc()
                self._log.warning(
                    "engine.batch_retry",
                    error=repr(exc),
                    reports=len(records),
                    retry_in_s=self._pause_s,
                )
            else:
                BATCHES.labels("failed").inc()
                self._log.exception(
                    "engine.batch_failed", reports=len(records), retry_in_s=self._pause_s
                )
            return _Outcome.RETRY
        self._backoff.reset()
        return _Outcome.DONE

    async def _reject(self, msg: Msg, rejected: Rejected) -> None:
        POISON.labels(rejected.reason).inc()
        self._log.warning(
            "engine.poison", reason=rejected.reason, detail=rejected.detail, subject=msg.subject
        )
        try:
            await msg.term()
        except Exception as exc:
            self._log.warning("engine.settle_failed", action="term", error=repr(exc))

    async def _settle(
        self, batch: Sequence[tuple[Msg, TelemetryRecord]], action: Literal["ack", "nak"]
    ) -> None:
        # A settle that does not reach the broker is not lost work: the message is redelivered
        # after the ack wait, arrives late, and changes nothing.
        try:
            for msg, _ in batch:
                await (msg.ack() if action == "ack" else msg.nak())
        except Exception as exc:
            self._log.warning("engine.settle_failed", action=action, error=repr(exc))

    async def _hand_back(self, sub: PullSubscription) -> None:
        """Give undelivered-to-the-batch messages back and drop the subscription.

        A message can land in the subscription's buffer just after a fetch timed out; without this
        it would sit there until its ack wait expired and then arrive out of order elsewhere.
        """
        with suppress(Exception):
            if sub.pending_msgs:
                for msg in await sub.fetch(sub.pending_msgs + 1, timeout=DRAIN_FETCH_TIMEOUT_S):
                    await msg.nak()
        with suppress(Exception):
            await sub.unsubscribe()

    async def _sleep(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)


def _trace_context(msgs: Iterable[Msg]) -> Mapping[str, str] | None:
    """Headers of the first message that carries a trace context, to continue that trace."""
    for msg in msgs:
        if msg.headers and "traceparent" in msg.headers:
            return msg.headers
    return None
