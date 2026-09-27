"""The partition worker's error paths, against real ``Msg`` objects over a recording transport."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from typing import Any, cast

import asyncpg
import pytest
import sqlalchemy.exc
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from prometheus_client import REGISTRY

from perimeter.bus.leases import FencingToken, Lease
from perimeter.domain.reports import TelemetryRecord
from perimeter.engine.batch import FencedOut, is_transient
from perimeter.engine.worker import (
    Backoff,
    InFlight,
    LeaseHandle,
    PartitionWorker,
    Rejected,
    WorkerExit,
    parse,
    problem_of,
)
from perimeter.wire import telemetry
from tests.support import eventually

T0 = 1_790_000_000_000


def record(device: str = "veh-1", at: int = T0, **fields: Any) -> TelemetryRecord:
    values: dict[str, Any] = {"lat": 52.37, "lon": 4.9, **fields}
    return TelemetryRecord(device, at, at, **values)


class Transport:
    """Stands in for the NATS client behind ``Msg``: records what each message sends back."""

    def __init__(self) -> None:
        self.sent: dict[str, list[bytes]] = {}

    async def publish(
        self, subject: str, payload: bytes = b"", reply: str = "", headers: object = None
    ) -> None:
        self.sent.setdefault(subject, []).append(payload)

    def verdicts(self, messages: Sequence[Msg]) -> list[str]:
        return [describe(self.sent.get(m.reply, [])) for m in messages]


def describe(payloads: list[bytes]) -> str:
    if not payloads:
        return "unsettled"
    assert len(payloads) == 1, f"settled more than once: {payloads}"
    payload = payloads[0]
    if payload == b"":
        return "ack"
    if payload == b"+TERM":
        return "term"
    if payload == b"-NAK":
        return "nak"
    if payload.startswith(b"-NAK "):
        delay_ns = json.loads(payload[5:])["delay"]
        return f"nak {delay_ns / 1e9:g}s"
    return f"unknown {payload!r}"


class Wire:
    def __init__(self) -> None:
        self.transport = Transport()
        self.sequence = 0

    def message(self, item: TelemetryRecord | bytes, *, subject: str | None = None) -> Msg:
        self.sequence += 1
        if isinstance(item, TelemetryRecord):
            data, subject = telemetry.encode(item), subject or f"tlm.0.{item.device_id}"
        else:
            data, subject = item, subject or "tlm.0.garbage"
        reply = f"$JS.ACK.TELEMETRY.engine-p0.1.{self.sequence}.{self.sequence}.{T0}000000.0"
        return Msg(
            _client=cast("NatsClient", self.transport), subject=subject, reply=reply, data=data
        )


class Subscription:
    """Serves prepared fetch results, then reports that nothing is left."""

    def __init__(self, *results: Sequence[Msg] | BaseException) -> None:
        self.results: deque[Sequence[Msg] | BaseException] = deque(results)
        self.fetches: list[float] = []
        self.timeouts: list[float | None] = []
        self.buffered: list[Msg] = []
        self.unsubscribed = False
        self.drained = asyncio.Event()

    @property
    def pending_msgs(self) -> int:
        return len(self.buffered)

    async def fetch(self, batch: int = 1, timeout: float | None = 5) -> list[Msg]:  # noqa: ASYNC109
        self.fetches.append(asyncio.get_running_loop().time())
        self.timeouts.append(timeout)
        if self.buffered:
            taken, self.buffered = self.buffered[:batch], self.buffered[batch:]
            return taken
        if not self.results:
            self.drained.set()
            await asyncio.sleep(min(timeout or 0.01, 0.01))
            raise TimeoutError
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return list(result)

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class Processor:
    """Records applied batches; fails the calls it is told to fail."""

    def __init__(self, *failures: BaseException | None) -> None:
        self.failures: deque[BaseException | None] = deque(failures)
        self.calls: list[tuple[int, FencingToken, list[TelemetryRecord]]] = []

    async def apply(
        self,
        partition: int,
        token: FencingToken,
        records: Sequence[TelemetryRecord],
        *,
        trace: Mapping[str, str] | None = None,
    ) -> object:
        self.calls.append((partition, token, list(records)))
        failure = self.failures.popleft() if self.failures else None
        if failure is not None:
            raise failure
        return None


def lease(revision: int = 5) -> LeaseHandle:
    return LeaseHandle(Lease("p.0", "engine-a", revision, 3), valid_until=math.inf)


async def nothing_in_flight() -> list[tuple[str, bytes]]:
    return []


def worker(
    subscription: Subscription,
    processor: Processor,
    handle: LeaseHandle | None = None,
    *,
    backoff_s: float = 0.05,
    in_flight: InFlight = nothing_in_flight,
) -> PartitionWorker:
    async def subscribe() -> Subscription:
        return subscription

    return PartitionWorker(
        0,
        subscribe=subscribe,
        in_flight=in_flight,
        processor=processor,
        lease=handle or lease(),
        batch_max=100,
        fetch_wait_s=0.05,
        backoff=Backoff(initial_s=backoff_s, cap_s=backoff_s * 4),
    )


async def run_until_drained(subject: PartitionWorker, subscription: Subscription) -> WorkerExit:
    task = asyncio.create_task(subject.run())
    drained = asyncio.create_task(subscription.drained.wait())
    await asyncio.wait({task, drained}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    subject.stop()
    drained.cancel()
    return await asyncio.wait_for(task, 5)


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_a_committed_batch_is_acked_message_by_message() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1")), wire.message(record("veh-2"))]
    subscription, processor = Subscription(batch), Processor()
    exit_ = await run_until_drained(worker(subscription, processor), subscription)
    assert exit_ is WorkerExit.STOPPED
    assert wire.transport.verdicts(batch) == ["ack", "ack"]
    assert [(p, t, [r.device_id for r in rs]) for p, t, rs in processor.calls] == [
        (0, lease().token, ["veh-1", "veh-2"])
    ]
    assert subscription.unsubscribed


async def test_messages_that_can_never_apply_are_terminated_as_poison() -> None:
    wire = Wire()
    before = metric("perimeter_engine_poison_total", reason="invalid")
    batch = [
        wire.message(b"\x93not-msgpack"),
        wire.message(record("veh-1"), subject="tlm.0.veh-2"),  # filed under another device
        wire.message(record("veh-3", lat=95.0)),
        wire.message(record("veh-4")),
    ]
    subscription, processor = Subscription(batch), Processor()
    await run_until_drained(worker(subscription, processor), subscription)
    assert wire.transport.verdicts(batch) == ["term", "term", "term", "ack"]
    assert [r.device_id for r in processor.calls[0][2]] == ["veh-4"]
    assert metric("perimeter_engine_poison_total", reason="invalid") == before + 2


async def test_a_batch_of_only_poison_never_reaches_the_database() -> None:
    wire = Wire()
    batch = [wire.message(b"\xc1")]
    subscription, processor = Subscription(batch), Processor()
    await run_until_drained(worker(subscription, processor), subscription)
    assert wire.transport.verdicts(batch) == ["term"]
    assert processor.calls == []


async def test_a_fenced_worker_hands_the_batch_back_at_once_and_stops() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1")), wire.message(record("veh-2"))]
    later = [wire.message(record("veh-3"))]
    subscription = Subscription(batch, later)
    processor = Processor(FencedOut("newer owner"))
    exit_ = await asyncio.wait_for(worker(subscription, processor).run(), 5)
    assert exit_ is WorkerExit.FENCED
    assert wire.transport.verdicts(batch) == ["nak", "nak"]  # no delay: the new owner needs them
    assert wire.transport.verdicts(later) == ["unsettled"]  # never fetched
    assert len(processor.calls) == 1


def in_flight_of(*items: TelemetryRecord | bytes, fail_first: int = 0) -> InFlight:
    """A predecessor's unacknowledged messages, as the stream would return them."""
    failures = [fail_first]

    async def read() -> list[tuple[str, bytes]]:
        if failures[0]:
            failures[0] -= 1
            raise TimeoutError("broker busy")
        return [
            (f"tlm.0.{i.device_id}", telemetry.encode(i))
            if isinstance(i, TelemetryRecord)
            else ("tlm.0.garbage", i)
            for i in items
        ]

    return read


async def test_a_new_owner_first_applies_what_its_predecessor_left_unacknowledged() -> None:
    wire = Wire()
    newer = [wire.message(record("veh-1", T0 + 2_000))]
    subscription, processor = Subscription(newer), Processor()
    before = metric("perimeter_engine_recovered_reports_total")
    left = in_flight_of(record("veh-1", T0), b"\xc1", record("veh-1", T0 + 1_000))
    await run_until_drained(worker(subscription, processor, in_flight=left), subscription)
    applied = [[r.recorded_at_ms - T0 for r in records] for _, _, records in processor.calls]
    assert applied == [[0, 1_000], [2_000]]  # recovered first, in order, poison skipped
    assert wire.transport.verdicts(newer) == ["ack"]
    assert metric("perimeter_engine_recovered_reports_total") == before + 2


async def test_recovery_retries_until_it_can_read_and_apply_the_backlog() -> None:
    wire = Wire()
    subscription = Subscription([wire.message(record("veh-2"))])
    processor = Processor(OSError("database restarting"))
    left = in_flight_of(record("veh-1"), fail_first=2)
    await run_until_drained(
        worker(subscription, processor, backoff_s=0.01, in_flight=left), subscription
    )
    assert [r.device_id for _, _, records in processor.calls for r in records] == [
        "veh-1",  # failed
        "veh-1",  # retried
        "veh-2",
    ]


async def test_a_large_backlog_is_recovered_in_bounded_batches() -> None:
    subscription, processor = Subscription(), Processor()
    backlog = [record(f"veh-{i}", T0 + i) for i in range(250)]
    subject = worker(subscription, processor, in_flight=in_flight_of(*backlog))
    await run_until_drained(subject, subscription)
    assert [len(records) for _, _, records in processor.calls] == [100, 100, 50]
    assert [r.device_id for _, _, rs in processor.calls for r in rs] == [
        r.device_id for r in backlog
    ]


async def test_a_worker_fenced_while_recovering_never_starts_fetching() -> None:
    wire = Wire()
    subscription = Subscription([wire.message(record("veh-2"))])
    processor = Processor(FencedOut("newer owner"))
    subject = worker(subscription, processor, in_flight=in_flight_of(record("veh-1")))
    assert await asyncio.wait_for(subject.run(), 5) is WorkerExit.FENCED
    assert subscription.fetches == []


async def test_a_transient_failure_hands_the_batch_back_and_pauses_before_fetching() -> None:
    wire = Wire()
    first = [wire.message(record("veh-1", T0))]
    second = [wire.message(record("veh-1", T0 + 1_000))]
    subscription = Subscription(first, second)
    processor = Processor(OSError("connection reset"))
    await run_until_drained(worker(subscription, processor, backoff_s=0.2), subscription)
    assert wire.transport.verdicts(first) == ["nak"]  # due again at once, served before newer ones
    assert wire.transport.verdicts(second) == ["ack"]
    pause = subscription.fetches[1] - subscription.fetches[0]
    assert pause >= 0.2, "the worker should back off before fetching again"


async def test_an_unexpected_failure_is_retried_rather_than_dropped() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1"))]
    again = [wire.message(record("veh-1"))]  # the broker redelivers it
    subscription = Subscription(batch, again)
    before = metric("perimeter_engine_batches_total", outcome="failed")
    processor = Processor(ValueError("a bug"))
    await run_until_drained(worker(subscription, processor, backoff_s=0.01), subscription)
    assert wire.transport.verdicts(batch) == ["nak"]
    assert wire.transport.verdicts(again) == ["ack"]
    assert metric("perimeter_engine_batches_total", outcome="failed") == before + 1


async def test_retry_pauses_grow_and_start_over_after_a_success() -> None:
    wire = Wire()
    failing = [[wire.message(record("veh-1"))] for _ in range(3)]
    fine = [wire.message(record("veh-1"))]
    failed_again = [wire.message(record("veh-1", T0 + 2))]
    last = [wire.message(record("veh-1", T0 + 3))]
    subscription = Subscription(*failing, fine, failed_again, last)
    processor = Processor(OSError(), OSError(), OSError(), None, OSError())
    subject = worker(subscription, processor, backoff_s=0.05)  # 0.05, 0.1, 0.2 (the cap)
    await run_until_drained(subject, subscription)
    fetched = subscription.fetches
    gaps = [later - earlier for earlier, later in pairwise(fetched)]
    assert gaps[0] >= 0.05
    assert gaps[1] >= 0.1
    assert gaps[2] >= 0.2
    assert gaps[4] < 0.15, "a success should reset the backoff"
    assert [wire.transport.verdicts(b)[0] for b in (*failing, fine, failed_again, last)] == [
        "nak",
        "nak",
        "nak",
        "ack",
        "nak",
        "ack",
    ]


async def test_fetch_errors_do_not_end_the_loop() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1"))]
    subscription = Subscription(ConnectionResetError("broker went away"), batch)
    processor = Processor()
    await run_until_drained(worker(subscription, processor, backoff_s=0.01), subscription)
    assert wire.transport.verdicts(batch) == ["ack"]


async def test_a_fetch_never_outlives_the_lease() -> None:
    # A pull request still open after the lease lapsed could deliver messages to this worker once
    # a new owner had taken the partition over.
    subscription, processor = Subscription(), Processor()
    handle = lease()

    async def subscribe() -> Subscription:
        return subscription

    subject = PartitionWorker(
        0,
        subscribe=subscribe,
        in_flight=nothing_in_flight,
        processor=processor,
        lease=handle,
        batch_max=100,
        fetch_wait_s=30.0,
        backoff=Backoff(initial_s=0.01, cap_s=0.04),
    )
    handle.refresh(handle.lease, valid_until=time.monotonic() + 0.3)
    task = asyncio.create_task(subject.run())
    await eventually(lambda: subscription.timeouts)
    subject.stop()
    await asyncio.wait_for(task, 5)
    first = subscription.timeouts[0]
    assert first is not None
    assert 0 < first <= 0.3


async def test_nothing_is_fetched_while_the_lease_is_not_known_to_be_valid() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1"))]
    handle = lease()
    handle.revoke()
    subscription, processor = Subscription(batch), Processor()
    subject = worker(subscription, processor, handle)
    task = asyncio.create_task(subject.run())
    await asyncio.sleep(0.3)
    assert subscription.fetches == []
    renewed = Lease("p.0", "engine-a", 9, 3)
    handle.refresh(renewed, valid_until=math.inf)
    await eventually(lambda: processor.calls)
    assert processor.calls[0][1] == renewed.token
    subject.stop()
    assert await asyncio.wait_for(task, 5) is WorkerExit.STOPPED


async def test_every_batch_is_fenced_with_the_current_token() -> None:
    wire = Wire()
    handle = lease(revision=5)
    first, second = [wire.message(record("veh-1"))], [wire.message(record("veh-2"))]
    subscription = Subscription(first, second)
    processor = Processor()
    original_apply = processor.apply

    async def apply_then_renew(*args: Any, **kwargs: Any) -> object:
        result = await original_apply(*args, **kwargs)
        handle.refresh(Lease("p.0", "engine-a", 42, 3), valid_until=math.inf)
        return result

    processor.apply = apply_then_renew  # type: ignore[method-assign]
    await run_until_drained(worker(subscription, processor, handle), subscription)
    tokens = [token for _, token, _ in processor.calls]
    assert tokens == [Lease("p.0", "x", 5, 3).token, Lease("p.0", "x", 42, 3).token]


async def test_stopping_hands_messages_that_arrived_too_late_back_to_the_broker() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1"))]
    straggler = wire.message(record("veh-2"))
    subscription, processor = Subscription(batch), Processor()
    subject = worker(subscription, processor)
    task = asyncio.create_task(subject.run())
    await subscription.drained.wait()  # the worker is inside a fetch that will time out...
    subscription.buffered.append(straggler)  # ...just as a message lands in its buffer
    subject.stop()
    assert await asyncio.wait_for(task, 5) is WorkerExit.STOPPED
    assert wire.transport.verdicts(batch) == ["ack"]
    assert wire.transport.verdicts([straggler]) == ["nak"]
    assert subscription.unsubscribed


async def test_stop_interrupts_a_retry_pause() -> None:
    wire = Wire()
    subscription = Subscription([wire.message(record("veh-1"))])
    processor = Processor(OSError())
    subject = worker(subscription, processor, backoff_s=30.0)
    task = asyncio.create_task(subject.run())
    await eventually(lambda: processor.calls)
    await asyncio.sleep(0.05)
    subject.stop()
    assert await asyncio.wait_for(task, 1.0) is WorkerExit.STOPPED


async def test_the_subscription_is_retried_until_it_binds() -> None:
    wire = Wire()
    batch = [wire.message(record("veh-1"))]
    subscription, processor = Subscription(batch), Processor()
    attempts = 0

    async def flaky() -> Subscription:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError
        return subscription

    subject = PartitionWorker(
        0,
        subscribe=flaky,
        in_flight=nothing_in_flight,
        processor=processor,
        lease=lease(),
        batch_max=10,
        fetch_wait_s=0.05,
        backoff=Backoff(initial_s=0.01),
    )
    await run_until_drained(subject, subscription)
    assert attempts == 3
    assert wire.transport.verdicts(batch) == ["ack"]


# --- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "subject", "problem"),
    [
        ({}, None, None),
        ({"speed": 0.0, "heading": 359.9, "accuracy": 3.5}, None, None),
        ({"lat": 90.0, "lon": -180.0}, None, None),
        ({"device_id": "veh 1"}, "tlm.0.veh 1", "device id is not a subject token"),
        ({"device_id": "x" * 65}, "tlm.0." + "x" * 65, "device id is not a subject token"),
        ({}, "tlm.0.veh-2", "device id does not match its subject"),
        ({"lat": math.nan}, None, "latitude out of range"),
        ({"lat": -90.5}, None, "latitude out of range"),
        ({"lon": math.inf}, None, "longitude out of range"),
        ({"recorded_at_ms": -1}, None, "recorded_at out of range"),
        ({"received_at_ms": 10**16}, None, "received_at out of range"),
        ({"speed": -0.1}, None, "speed out of range"),
        ({"heading": 360.5}, None, "heading out of range"),
        ({"accuracy": math.nan}, None, "accuracy out of range"),
    ],
)
def test_validation_rejects_only_what_can_never_be_applied(
    changes: dict[str, Any], subject: str | None, problem: str | None
) -> None:
    fields: dict[str, Any] = {
        "device_id": "veh-1",
        "recorded_at_ms": T0,
        "received_at_ms": T0,
        "lat": 52.37,
        "lon": 4.9,
        **changes,
    }
    candidate = TelemetryRecord(**fields)
    assert problem_of(candidate, subject or f"tlm.3.{candidate.device_id}") == problem


def test_parse_tells_records_from_messages_that_can_never_apply() -> None:
    good = record("veh-1")
    assert parse("tlm.4.veh-1", telemetry.encode(good)) == good
    assert parse("tlm.4.veh-1", b"\xc1").reason == "undecodable"  # type: ignore[union-attr]
    assert parse("tlm.4.veh-9", telemetry.encode(good)) == Rejected(
        "invalid", "device id does not match its subject"
    )


def test_backoff_doubles_up_to_its_cap_and_resets() -> None:
    backoff = Backoff(initial_s=1.0, cap_s=5.0)
    assert [backoff.next() for _ in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    backoff.reset()
    assert backoff.next() == 1.0


def test_lease_handles_follow_renewals_and_revocation() -> None:
    handle = LeaseHandle(Lease("p.1", "a", 3, 2), valid_until=10.0)
    assert handle.valid(9.9)
    assert not handle.valid(10.0)
    handle.refresh(Lease("p.1", "a", 4, 2), valid_until=20.0)
    assert handle.token == FencingToken(generation=2, revision=4)
    assert handle.valid(19.9)
    handle.revoke()
    assert not handle.valid(-1e12)


def _pg_error(error: type[Exception]) -> sqlalchemy.exc.DBAPIError:
    try:
        try:
            raise error("simulated")
        except Exception as cause:
            raise sqlalchemy.exc.OperationalError("SELECT 1", {}, cause) from cause
    except sqlalchemy.exc.DBAPIError as wrapped:
        return wrapped


@pytest.mark.parametrize(
    ("make", "transient"),
    [
        (TimeoutError, True),
        (ConnectionRefusedError, True),
        (lambda: sqlalchemy.exc.TimeoutError("pool exhausted"), True),
        (lambda: _pg_error(asyncpg.exceptions.SerializationError), True),
        (lambda: _pg_error(asyncpg.exceptions.DeadlockDetectedError), True),
        (lambda: _pg_error(asyncpg.exceptions.LockNotAvailableError), True),
        (lambda: _pg_error(asyncpg.exceptions.QueryCanceledError), True),
        (lambda: _pg_error(asyncpg.exceptions.ConnectionDoesNotExistError), True),
        (lambda: _pg_error(asyncpg.exceptions.TooManyConnectionsError), True),
        (lambda: _pg_error(asyncpg.exceptions.CannotConnectNowError), True),
        (lambda: _pg_error(asyncpg.exceptions.UndefinedTableError), False),
        (lambda: _pg_error(asyncpg.exceptions.CheckViolationError), False),
        (lambda: ValueError("a bug"), False),
    ],
)
def test_transient_errors_are_told_apart_from_defects(
    make: Callable[[], BaseException], transient: bool
) -> None:
    assert is_transient(make()) is transient


async def test_a_drained_partition_lingers_so_batches_grow() -> None:
    wire = Wire()
    partial = [wire.message(record(f"veh-{i}")) for i in range(3)]
    later = [wire.message(record(f"veh-{i}")) for i in range(3, 5)]
    subscription = Subscription(partial, later)
    processor = Processor()

    async def subscribe() -> Subscription:
        return subscription

    subject = PartitionWorker(
        0,
        subscribe=subscribe,
        in_flight=nothing_in_flight,
        processor=processor,
        lease=lease(),
        batch_max=100,
        fetch_wait_s=0.05,
        linger_s=0.2,
    )
    await run_until_drained(subject, subscription)
    assert [len(records) for _, _, records in processor.calls] == [3, 2]
    first, second = subscription.fetches[:2]
    assert second - first >= 0.2  # waited for more reports after a partial batch


async def test_a_full_batch_is_followed_by_an_immediate_fetch() -> None:
    wire = Wire()
    full = [wire.message(record(f"veh-{i}")) for i in range(10)]
    subscription = Subscription(full, [wire.message(record("veh-x"))])
    processor = Processor()

    async def subscribe() -> Subscription:
        return subscription

    subject = PartitionWorker(
        0,
        subscribe=subscribe,
        in_flight=nothing_in_flight,
        processor=processor,
        lease=lease(),
        batch_max=10,
        fetch_wait_s=0.05,
        linger_s=5.0,
    )
    task = asyncio.create_task(subject.run())
    await eventually(lambda: len(subscription.fetches) >= 2, within=2.0)
    first, second = subscription.fetches[:2]
    assert second - first < 1.0  # a backlog is consumed without lingering
    subject.stop()  # interrupts the linger that follows the partial second batch
    assert await asyncio.wait_for(task, 2.0) is WorkerExit.STOPPED
