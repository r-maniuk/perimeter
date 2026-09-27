"""Partition ownership: the pure round planner, and the coordinator against an in-memory bucket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from perimeter.bus.leases import Lease, LeaseLost
from perimeter.domain.clock import ManualClock
from perimeter.domain.rendezvous import assignment
from perimeter.engine.coordinator import (
    MEMBER_PREFIX,
    Coordinator,
    Plan,
    lease_key,
    member_key,
    plan,
)
from perimeter.engine.worker import LeaseHandle, WorkerExit
from tests.support import eventually

TTL = 6.0
INTERVAL = TTL / 3


def decide(
    me: str, members: list[str], held: dict[int, float], *, now: float, partitions: int = 16
) -> Plan:
    return plan(
        me=me,
        members=members,
        partitions=partitions,
        held=held,
        now=now,
        ttl_s=TTL,
        interval_s=INTERVAL,
    )


# --- plan: the decision of one round ---------------------------------------------------------


def test_a_lone_instance_wants_every_partition() -> None:
    decision = decide("a", [], {}, now=0.0, partitions=4)
    assert decision.desired == decision.acquire == frozenset(range(4))
    assert not decision.renew | decision.release | decision.expire


def test_a_joining_member_takes_exactly_its_rendezvous_share() -> None:
    split = assignment(16, ["a", "b"])
    decision = decide("a", ["a", "b"], dict.fromkeys(range(16), 0.0), now=1.0)
    assert decision.release == split["b"]
    assert decision.renew == frozenset(range(16))  # still renewed while they drain
    assert decision.acquire == frozenset()
    assert decision.desired == split["a"]


def test_a_departed_member_leaves_its_share_to_the_rest() -> None:
    split = assignment(16, ["a", "b"])
    decision = decide("a", ["a"], dict.fromkeys(split["a"], 0.0), now=1.0)
    assert decision.acquire == split["b"]
    assert decision.release == frozenset()


def test_the_planner_counts_itself_even_when_absent_from_the_bucket() -> None:
    decision = decide("a", ["b"], {}, now=0.0)
    assert decision.desired == assignment(16, ["a", "b"])["a"]


def test_a_lease_not_renewed_in_time_is_presumed_lost() -> None:
    decision = decide("a", ["a"], {0: 0.0, 1: 3.0}, now=TTL - INTERVAL, partitions=2)
    assert decision.expire == {0}
    assert decision.renew == {1}
    assert decision.acquire == frozenset()  # re-acquired once the broker has expired it


names = st.text(alphabet="abcdefgh", min_size=1, max_size=3)


@given(
    me=names,
    members=st.lists(names, max_size=5),
    held=st.dictionaries(st.integers(0, 15), st.floats(0, 10)),
    now=st.floats(0, 20),
)
def test_every_held_lease_is_either_kept_or_expired_and_nothing_is_acquired_twice(
    me: str, members: list[str], held: dict[int, float], now: float
) -> None:
    decision = decide(me, members, held, now=now)
    assert decision.renew | decision.expire == frozenset(held)
    assert not decision.renew & decision.expire
    assert decision.release <= decision.renew
    assert decision.release.isdisjoint(decision.desired)
    assert decision.acquire.isdisjoint(held)
    assert decision.acquire == decision.desired - frozenset(held)


@given(members=st.lists(names, min_size=1, max_size=6, unique=True))
def test_members_agreeing_on_the_membership_never_desire_the_same_partition(
    members: list[str],
) -> None:
    desired = [decide(me, members, {}, now=0.0).desired for me in members]
    assert frozenset().union(*desired) == frozenset(range(16))
    assert sum(len(d) for d in desired) == 16


# --- the coordinator against an in-memory bucket -----------------------------------------------


class FakeBucket:
    """The broker's lease semantics in memory: compare-and-set by revision, expiry after TTL."""

    def __init__(self, clock: ManualClock, ttl_s: float = TTL) -> None:
        self.clock = clock
        self.ttl_s = ttl_s
        self.revision = 0
        self.entries: dict[str, tuple[bytes, int, float]] = {}
        self.failing: set[str] = set()

    def _live(self, key: str) -> tuple[bytes, int, float] | None:
        entry = self.entries.get(key)
        if entry is not None and self.clock.monotonic() - entry[2] >= self.ttl_s:
            del self.entries[key]
            return None
        return entry

    def write(self, key: str, value: bytes) -> int:
        self.revision += 1
        self.entries[key] = (value, self.revision, self.clock.monotonic())
        return self.revision

    def _check(self, operation: str) -> None:
        if operation in self.failing:
            raise TimeoutError(operation)

    def holder(self, key: str) -> str | None:
        entry = self._live(key)
        return entry[0].decode() if entry is not None else None

    async def acquire(self, key: str, owner: str) -> Lease | None:
        self._check("acquire")
        if self._live(key) is not None:
            return None
        return Lease(key, owner, self.write(key, owner.encode()), generation=7)

    async def renew(self, lease: Lease) -> Lease:
        self._check("renew")
        entry = self._live(lease.key)
        if entry is None or entry[1] != lease.revision:
            raise LeaseLost(lease.key)
        revision = self.write(lease.key, lease.owner.encode())
        return Lease(lease.key, lease.owner, revision, lease.generation)

    async def release(self, lease: Lease) -> None:
        self._check("release")
        entry = self._live(lease.key)
        if entry is not None and entry[1] == lease.revision:
            del self.entries[lease.key]

    async def heartbeat(self, key: str, value: bytes) -> None:
        self._check("heartbeat")
        self.write(key, value)

    async def keys(self, prefix: str) -> list[str]:
        self._check("keys")
        return sorted(k for k in list(self.entries) if k.startswith(prefix) and self._live(k))

    async def remove(self, key: str) -> None:
        self.entries.pop(key, None)


class FakeWorker:
    def __init__(self, partition: int, lease: LeaseHandle) -> None:
        self.partition = partition
        self.lease = lease
        self.stopped = asyncio.Event()
        self.exit: WorkerExit | BaseException = WorkerExit.STOPPED

    def stop(self) -> None:
        self.stopped.set()

    async def run(self) -> WorkerExit:
        await self.stopped.wait()
        if isinstance(self.exit, BaseException):
            raise self.exit
        return self.exit


class Cluster:
    """Coordinators of several instances sharing one bucket and one manual clock."""

    def __init__(self, partitions: int = 8) -> None:
        self.partitions = partitions
        self.clock = ManualClock()
        self.bucket = FakeBucket(self.clock)
        self.workers: dict[str, dict[int, FakeWorker]] = {}
        self.coordinators: list[Coordinator] = []

    def join(self, instance: str) -> Coordinator:
        self.workers[instance] = {}

        def make(partition: int, lease: LeaseHandle) -> FakeWorker:
            worker = FakeWorker(partition, lease)
            self.workers[instance][partition] = worker
            return worker

        coordinator = Coordinator(
            instance=instance,
            partitions=self.partitions,
            leases=self.bucket,
            workers=make,
            ttl_s=TTL,
            clock=self.clock,
        )
        self.coordinators.append(coordinator)
        return coordinator

    def holders(self) -> dict[int, str | None]:
        return {p: self.bucket.holder(lease_key(p)) for p in range(self.partitions)}


@pytest.fixture
async def new_cluster() -> AsyncIterator[Callable[..., Cluster]]:
    made: list[Cluster] = []

    def make(partitions: int = 8) -> Cluster:
        made.append(Cluster(partitions))
        return made[-1]

    yield make
    for cluster in made:
        for coordinator in cluster.coordinators:
            await coordinator.abort()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def rounds(*coordinators: Coordinator, times: int = 1) -> None:
    for _ in range(times):
        for coordinator in coordinators:
            await coordinator.tick()
            await settle()


async def test_a_single_instance_takes_every_partition(new_cluster: Callable[..., Cluster]) -> None:
    cluster = new_cluster()
    a = cluster.join("a")
    await rounds(a)
    assert a.owned == list(range(8))
    assert set(cluster.holders().values()) == {"a"}
    assert await cluster.bucket.keys(MEMBER_PREFIX) == [member_key("a")]


async def test_partitions_are_split_when_a_second_instance_joins(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster()
    a, b = cluster.join("a"), cluster.join("b")
    await rounds(a)
    split = assignment(8, ["a", "b"])
    for _ in range(3):
        await rounds(b, a)
        assert not set(a.owned) & set(b.owned), "a partition was consumed twice"
    assert a.owned == sorted(split["a"])
    assert b.owned == sorted(split["b"])
    assert all(cluster.workers["a"][p].stopped.is_set() for p in split["b"])


async def test_a_departing_instance_hands_its_partitions_over_without_waiting(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster()
    a, b = cluster.join("a"), cluster.join("b")
    await rounds(a, b, a, b, a, b)
    assert a.owned
    assert b.owned
    await a.shutdown(grace_s=1.0)
    await rounds(b)  # no time has passed: nothing had to expire
    assert b.owned == list(range(8))
    assert await cluster.bucket.keys(MEMBER_PREFIX) == [member_key("b")]


async def test_a_crashed_instance_is_replaced_once_its_leases_expire(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster()
    a, b = cluster.join("a"), cluster.join("b")
    await rounds(a, b, a, b, a, b)
    theirs = set(a.owned)
    await a.abort()  # no release, no goodbye: the keys stay until the TTL runs out
    for _ in range(2):
        cluster.clock.advance(INTERVAL)
        await rounds(b)
        assert set(b.owned).isdisjoint(theirs)
    cluster.clock.advance(INTERVAL)  # a full TTL since a's last write
    await rounds(b)
    assert b.owned == list(range(8))


async def test_a_lost_lease_stops_its_worker_at_once(new_cluster: Callable[..., Cluster]) -> None:
    cluster = new_cluster(partitions=2)
    a = cluster.join("a")
    await rounds(a)
    worker = cluster.workers["a"][1]
    cluster.bucket.write(lease_key(1), b"someone-else")  # somebody else holds it now
    await rounds(a)
    assert worker.stopped.is_set()
    assert not worker.lease.valid(cluster.clock.monotonic())
    assert a.owned == [0]


async def test_a_worker_keeps_running_through_broker_trouble_only_while_the_lease_is_safe(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster(partitions=2)
    a = cluster.join("a")
    await rounds(a)
    workers = dict(cluster.workers["a"])
    cluster.bucket.failing = {"heartbeat", "keys", "renew"}
    cluster.clock.advance(INTERVAL)
    await rounds(a)
    assert a.owned == [0, 1]
    assert all(w.lease.valid(cluster.clock.monotonic()) for w in workers.values())
    cluster.clock.advance(INTERVAL)  # TTL - interval since the last renewal: presume lost
    await rounds(a)
    assert a.owned == []
    assert all(w.stopped.is_set() for w in workers.values())
    assert not any(w.lease.valid(cluster.clock.monotonic()) for w in workers.values())


async def test_renewals_raise_the_fencing_token_the_worker_sees(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster(partitions=1)
    a = cluster.join("a")
    await rounds(a)
    handle = cluster.workers["a"][0].lease
    first = handle.token
    cluster.clock.advance(INTERVAL)
    await rounds(a)
    assert handle.token > first
    assert handle.valid(cluster.clock.monotonic())


async def test_a_fenced_worker_gives_its_partition_up_until_the_lease_is_free(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster(partitions=1)
    a = cluster.join("a")
    await rounds(a)
    fenced = cluster.workers["a"][0]
    fenced.exit = WorkerExit.FENCED
    fenced.stop()
    await settle()
    await rounds(a)
    assert a.owned == []  # the lease key still exists, so it cannot be taken again yet
    cluster.clock.advance(TTL)
    await rounds(a)
    assert a.owned == [0]
    assert cluster.workers["a"][0] is not fenced


async def test_a_crashed_worker_is_replaced_by_a_fresh_one(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster(partitions=1)
    a = cluster.join("a")
    await rounds(a)
    crashed = cluster.workers["a"][0]
    crashed.exit = RuntimeError("boom")
    crashed.stop()
    await settle()
    await rounds(a)
    assert a.owned == [0]
    assert cluster.workers["a"][0] is not crashed


async def test_a_partition_is_not_restarted_while_its_previous_worker_winds_down(
    new_cluster: Callable[..., Cluster],
) -> None:
    cluster = new_cluster(partitions=1)
    a = cluster.join("a")
    await rounds(a)
    slow = cluster.workers["a"][0]
    slow.stop = lambda: None  # type: ignore[method-assign]  # stuck finishing a batch
    cluster.bucket.write(lease_key(0), b"someone-else")
    await rounds(a)
    assert a.owned == []
    cluster.clock.advance(TTL)
    await rounds(a)
    assert a.owned == [], "a second worker was started next to the first"
    slow.stopped.set()
    await settle()
    await rounds(a)
    assert a.owned == [0]


async def test_shutdown_gives_everything_back(new_cluster: Callable[..., Cluster]) -> None:
    cluster = new_cluster(partitions=4)
    a = cluster.join("a")
    await rounds(a)
    await a.shutdown(grace_s=1.0)
    assert cluster.bucket.entries == {}
    assert a.owned == []


async def test_changes_that_move_partitions_start_a_round_at_once() -> None:
    bucket = FakeBucket(ManualClock(), ttl_s=60.0)  # rounds every 20 s unless woken
    a = Coordinator(instance="a", partitions=4, leases=bucket, workers=FakeWorker, ttl_s=60.0)
    stop = asyncio.Event()
    loop = asyncio.create_task(a.run(stop))
    try:
        await wait_for(lambda: a.owned == [0, 1, 2, 3])
        await bucket.heartbeat(member_key("b"), b"{}")
        for key, operation in [(member_key("a"), None), (lease_key(3), None)]:
            a.notice(key, operation)  # our own heartbeat, a renewal: nothing moves
        await asyncio.sleep(0.1)
        assert a.members == ["a"]
        a.notice(member_key("b"), None)  # a newcomer
        await wait_for(lambda: a.members == ["a", "b"])
        await bucket.remove(member_key("b"))
        a.notice(member_key("b"), "DEL")  # a departure
        await wait_for(lambda: a.members == ["a"])
    finally:
        stop.set()
        await asyncio.wait_for(loop, 1.0)
        await a.shutdown(grace_s=1.0)


async def test_the_round_loop_keeps_leases_alive_until_stopped() -> None:
    bucket = FakeBucket(ManualClock(), ttl_s=0.3)
    coordinator = Coordinator(
        instance="a", partitions=3, leases=bucket, workers=FakeWorker, ttl_s=0.3
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(coordinator.run(stop))
    await wait_for(lambda: coordinator.owned == [0, 1, 2])
    revision = bucket.revision
    await wait_for(lambda: bucket.revision >= revision + 6)  # renewals of 3 leases, twice
    assert coordinator.owned == [0, 1, 2]
    stop.set()
    await asyncio.wait_for(loop, 1.0)
    await coordinator.shutdown(grace_s=1.0)
    assert bucket.entries == {}


async def wait_for(condition: Callable[[], bool], within: float = 2.0) -> None:
    await eventually(condition, within=within, interval=0.01)
