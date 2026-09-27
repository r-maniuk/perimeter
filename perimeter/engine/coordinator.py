"""Partition ownership: which engine instance consumes which telemetry partition.

Instances agree without talking to each other:

1. membership: each instance keeps ``m.<instance>`` alive in the ``engine`` KV bucket; entries
   expire after the bucket TTL, so a crashed instance drops out on its own;
2. assignment: rendezvous hashing over the live members (:mod:`perimeter.domain.rendezvous`)
   gives every instance the same answer, and a membership change moves only the partitions the
   joining or leaving member wins or loses;
3. leases: consuming partition ``p`` also requires the lease ``p.<p>``, created with
   compare-and-set and renewed by revision. Rendezvous says who *should* own a partition; the
   lease makes sure at most one instance *does*, even while members briefly disagree about the
   membership. The lease revision is the fencing token every batch presents to the database.

Each round is decided by a pure function of (members, held leases, clock), :func:`plan`, tested
without a broker; :class:`Coordinator` carries the decision out. Rounds run every TTL/3 and, in
between, as soon as the bucket shows a change that moves partitions (a member joining or leaving,
a lease given back), so a graceful handover takes milliseconds rather than a round.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

import msgspec
import structlog

from perimeter.bus.leases import Lease, LeaseLost
from perimeter.domain import rendezvous
from perimeter.domain.clock import SYSTEM_CLOCK, Clock
from perimeter.engine.metrics import LEASE_EVENTS, PARTITIONS_OWNED
from perimeter.engine.worker import LeaseHandle, WorkerExit

log = structlog.get_logger(__name__)

MEMBER_PREFIX = "m."
LEASE_PREFIX = "p."
DELETE_OPERATIONS = frozenset({"DEL", "PURGE"})


def member_key(instance: str) -> str:
    return f"{MEMBER_PREFIX}{instance}"


def lease_key(partition: int) -> str:
    return f"{LEASE_PREFIX}{partition}"


@dataclass(frozen=True, slots=True)
class Plan:
    """What one round does with each partition."""

    desired: frozenset[int]  # partitions rendezvous hashing assigns to this instance
    acquire: frozenset[int]  # desired, lease not held yet
    renew: frozenset[int]  # lease held and still in time (including those being released)
    release: frozenset[int]  # lease held but no longer desired: drain the worker, give it back
    expire: frozenset[int]  # lease held but not renewed in time: presume it lost, stop at once


def plan(
    *,
    me: str,
    members: Iterable[str],
    partitions: int,
    held: Mapping[int, float],
    now: float,
    ttl_s: float,
    interval_s: float,
) -> Plan:
    """Decide one round.

    ``held`` maps every partition whose lease this instance holds to when the lease was last
    written (acquired or renewed) on the monotonic clock. A lease not renewed for ``ttl_s -
    interval_s`` is presumed lost: the broker may expire it before the next round, and acting a
    round early is the only way never to act a round late.
    """
    live = frozenset(members) | {me}
    desired = frozenset(p for p in range(partitions) if rendezvous.owner(p, live) == me)
    expire = frozenset(p for p, written in held.items() if now - written >= ttl_s - interval_s)
    alive = frozenset(held) - expire
    return Plan(
        desired=desired,
        acquire=desired - frozenset(held),
        renew=alive,
        release=alive - desired,
        expire=expire,
    )


class Leases(Protocol):
    """What the coordinator needs of :class:`perimeter.bus.leases.LeaseBucket`."""

    async def acquire(self, key: str, owner: str) -> Lease | None: ...

    async def renew(self, lease: Lease) -> Lease: ...

    async def release(self, lease: Lease) -> None: ...

    async def heartbeat(self, key: str, value: bytes) -> None: ...

    async def keys(self, prefix: str) -> list[str]: ...

    async def remove(self, key: str) -> None: ...


class Worker(Protocol):
    def stop(self) -> None: ...

    async def run(self) -> WorkerExit: ...


WorkerFactory = Callable[[int, LeaseHandle], Worker]


@dataclass(slots=True, eq=False)
class _Owned:
    handle: LeaseHandle
    renewed_at: float
    worker: Worker
    task: asyncio.Task[WorkerExit]
    releasing: bool = False


class Coordinator:
    """Keeps this instance's share of the partitions leased and consumed."""

    def __init__(
        self,
        *,
        instance: str,
        partitions: int,
        leases: Leases,
        workers: WorkerFactory,
        ttl_s: float,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._instance = instance
        self._partitions = partitions
        self._leases = leases
        self._workers = workers
        self._ttl_s = ttl_s
        self._interval_s = ttl_s / 3
        self._clock = clock
        self._members: frozenset[str] = frozenset({instance})
        self._owned: dict[int, _Owned] = {}
        self._retiring: dict[int, asyncio.Task[WorkerExit]] = {}
        self._wake = asyncio.Event()
        self._beacon = msgspec.json.encode({"instance": instance, "since": time.time()})
        self._log = log.bind(instance=instance)

    @property
    def owned(self) -> list[int]:
        """Partitions this instance consumes right now (including ones being handed over)."""
        return sorted(self._owned)

    @property
    def members(self) -> list[str]:
        return sorted(self._members)

    @property
    def interval_s(self) -> float:
        return self._interval_s

    def notice(self, key: str, operation: str | None) -> None:
        """React to a change in the bucket: wake up early if it can move partitions."""
        if operation in DELETE_OPERATIONS or (
            key.startswith(MEMBER_PREFIX) and key.removeprefix(MEMBER_PREFIX) not in self._members
        ):
            self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Run rounds every TTL/3, or sooner when woken, until ``stop`` is set."""
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            self._wake.clear()
            started = loop.time()
            try:
                await self.tick()
            except Exception:
                self._log.exception("engine.round_failed")
            remaining = self._interval_s - (loop.time() - started)
            await _first_of(stop, self._wake, within_s=max(0.0, remaining))

    async def tick(self) -> None:
        """One round: refresh the membership, then acquire, renew and release as planned."""
        await self._refresh_members()
        await self._reap()
        decision = plan(
            me=self._instance,
            members=self._members,
            partitions=self._partitions,
            held={partition: owned.renewed_at for partition, owned in self._owned.items()},
            now=self._clock.monotonic(),
            ttl_s=self._ttl_s,
            interval_s=self._interval_s,
        )
        for partition in sorted(decision.expire):
            self._abandon(partition, "expired")
        await self._renew(decision.renew)
        for partition in sorted(decision.release):
            self._begin_release(partition)
        await self._acquire(decision.acquire)
        PARTITIONS_OWNED.set(len(self._owned))

    async def shutdown(self, *, grace_s: float) -> None:
        """Drain every worker, give every lease back and leave the membership.

        Peers see the deleted keys and take the partitions over at once instead of waiting for
        the leases to expire.
        """
        # Entries stay registered until their lease is given back, so that an abort() arriving
        # while this waits (a second signal) still finds and cancels every worker.
        for entry in self._owned.values():
            entry.worker.stop()
        tasks = [entry.task for entry in self._owned.values()] + list(self._retiring.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_s)
            for task in pending:
                self._log.warning("engine.worker_drain_timeout", task=task.get_name())
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)
        await asyncio.gather(
            *(
                self._release(partition, entry.handle.lease)
                for partition, entry in sorted(self._owned.items())
                if _exit_of(entry.task) is not WorkerExit.FENCED
            )
        )
        self._owned.clear()
        self._retiring.clear()
        try:
            await self._leases.remove(member_key(self._instance))
        except Exception as exc:
            self._log.warning("engine.leave_failed", error=repr(exc))
        PARTITIONS_OWNED.set(0)

    async def abort(self) -> None:
        """Stop every worker abruptly and keep the leases; they expire on the broker's clock."""
        tasks = [entry.task for entry in self._owned.values()] + list(self._retiring.values())
        self._owned.clear()
        self._retiring.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        PARTITIONS_OWNED.set(0)

    async def _refresh_members(self) -> None:
        try:
            await self._leases.heartbeat(member_key(self._instance), self._beacon)
            keys = await self._leases.keys(MEMBER_PREFIX)
        except Exception as exc:  # keep the last known membership; leases decide safety
            self._log.warning("engine.membership_unavailable", error=repr(exc))
            return
        members = frozenset(key.removeprefix(MEMBER_PREFIX) for key in keys) | {self._instance}
        if members != self._members:
            self._log.info("engine.members_changed", members=sorted(members))
        self._members = members

    async def _reap(self) -> None:
        """Settle partitions whose worker has exited: give their lease back, or forget it."""
        for partition, entry in list(self._owned.items()):
            if not entry.task.done():
                continue
            del self._owned[partition]
            outcome = _exit_of(entry.task)
            if outcome is WorkerExit.FENCED:  # the lease belongs to a newer owner by now
                LEASE_EVENTS.labels("fenced").inc()
                self._log.warning("engine.partition_fenced", partition=partition)
                continue
            if isinstance(outcome, BaseException):
                self._log.error("engine.worker_crashed", partition=partition, exc_info=outcome)
            await self._release(partition, entry.handle.lease)
        for partition, task in list(self._retiring.items()):
            if task.done():
                del self._retiring[partition]
                outcome = _exit_of(task)
                if isinstance(outcome, BaseException):
                    self._log.error("engine.worker_crashed", partition=partition, exc_info=outcome)

    def _abandon(self, partition: int, reason: str) -> None:
        """The lease is (presumed) lost: stop the worker before it fetches or commits again."""
        entry = self._owned.pop(partition)
        entry.handle.revoke()
        entry.worker.stop()
        self._retiring[partition] = entry.task
        LEASE_EVENTS.labels(reason).inc()
        self._log.warning("engine.partition_lost", partition=partition, reason=reason)

    async def _renew(self, partitions: Iterable[int]) -> None:
        async def renew(partition: int) -> None:
            entry = self._owned.get(partition)
            if entry is None:
                return
            sent = self._clock.monotonic()
            try:
                lease = await self._leases.renew(entry.handle.lease)
            except LeaseLost:
                if self._owned.get(partition) is entry:
                    self._abandon(partition, "lost")
                return
            except Exception as exc:  # the expiry rule in plan() decides when this is fatal
                self._log.warning("engine.renew_failed", partition=partition, error=repr(exc))
                return
            entry.renewed_at = sent
            entry.handle.refresh(lease, valid_until=self._valid_until(sent))

        await asyncio.gather(*(renew(partition) for partition in sorted(partitions)))

    def _begin_release(self, partition: int) -> None:
        entry = self._owned[partition]
        if not entry.releasing:
            entry.releasing = True
            entry.worker.stop()
            self._log.info("engine.partition_draining", partition=partition)

    async def _acquire(self, partitions: Iterable[int]) -> None:
        async def acquire(partition: int) -> None:
            retiring = self._retiring.get(partition)
            if retiring is not None and not retiring.done():
                return  # the previous worker of this partition is still winding down
            sent = self._clock.monotonic()
            try:
                lease = await self._leases.acquire(lease_key(partition), self._instance)
            except Exception as exc:
                self._log.warning("engine.acquire_failed", partition=partition, error=repr(exc))
                return
            if lease is not None:  # otherwise its previous owner still holds it, for now
                self._start(partition, lease, sent)

        await asyncio.gather(*(acquire(partition) for partition in sorted(partitions)))

    def _start(self, partition: int, lease: Lease, sent: float) -> None:
        handle = LeaseHandle(lease, valid_until=self._valid_until(sent))
        worker = self._workers(partition, handle)
        task = asyncio.create_task(worker.run(), name=f"engine-partition-{partition}")
        task.add_done_callback(lambda _: self._wake.set())
        self._owned[partition] = _Owned(handle, sent, worker, task)
        LEASE_EVENTS.labels("acquired").inc()
        self._log.info("engine.partition_acquired", partition=partition, token=lease.token)

    async def _release(self, partition: int, lease: Lease) -> None:
        try:
            await self._leases.release(lease)
        except Exception as exc:  # not fatal: the lease expires on its own
            self._log.warning("engine.release_failed", partition=partition, error=repr(exc))
            return
        LEASE_EVENTS.labels("released").inc()
        self._log.info("engine.partition_released", partition=partition)

    def _valid_until(self, written_at: float) -> float:
        return written_at + self._ttl_s - self._interval_s


def _exit_of(task: asyncio.Task[WorkerExit]) -> WorkerExit | BaseException | None:
    if not task.done() or task.cancelled():
        return None
    return task.exception() or task.result()


async def _first_of(*events: asyncio.Event, within_s: float) -> None:
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=within_s, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
