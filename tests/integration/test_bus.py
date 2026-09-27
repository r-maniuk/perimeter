"""Broker topology, leases and the outbox relay against a real NATS server."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import KeyValueConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.bus.leases import FencingToken, LeaseBucket, LeaseLost, generation_of
from perimeter.bus.relay import OutboxRelay
from perimeter.storage import outbox
from perimeter.storage.outbox import PendingEvent
from perimeter.wire import subjects
from tests.support import eventually


async def test_ensure_is_idempotent_and_verify_accepts_it(
    js: JetStreamContext, topo: topology.Topology
) -> None:
    await topology.ensure(js, topo)
    await topology.ensure(js, topo)
    await topology.verify(js, topo)
    info = await js.stream_info(subjects.TELEMETRY_STREAM)
    assert info.config.subjects == [subjects.TELEMETRY_INPUT]
    consumers = await js.consumers_info(subjects.TELEMETRY_STREAM)
    assert sorted(c.name for c in consumers) == [f"engine-p{p}" for p in range(topo.partitions)]


async def test_repartitioning_is_refused(js: JetStreamContext, topo: topology.Topology) -> None:
    await topology.ensure(js, topo)
    other = topology.Topology(partitions=topo.partitions * 2)
    with pytest.raises(topology.TopologyError, match="partitions"):
        await topology.verify(js, other)
    with pytest.raises(topology.TopologyError, match="partitions"):
        await topology.ensure(js, other)


async def test_verify_requires_provisioning(js: JetStreamContext, topo: topology.Topology) -> None:
    with pytest.raises(topology.TopologyError, match="init job"):
        await topology.verify(js, topo)


async def test_reports_of_one_device_always_land_in_the_same_partition(
    js: JetStreamContext, provisioned: topology.Topology
) -> None:
    for round_ in range(3):
        for device in ("veh-1", "veh-2", "bike-9", "ped-33"):
            await js.publish(subjects.telemetry(device), f"{round_}".encode())
    info = await js.stream_info(subjects.TELEMETRY_STREAM, subjects_filter=">")
    stored = info.state.subjects or {}
    assert sum(stored.values()) == 12
    by_device: dict[str, set[int]] = {}
    for subject, count in stored.items():
        assert count == 3
        by_device.setdefault(subjects.device_of(subject), set()).add(subjects.partition_of(subject))
    assert all(len(parts) == 1 for parts in by_device.values())
    assert all(0 <= next(iter(p)) < provisioned.partitions for p in by_device.values())


async def test_events_are_republished_with_their_per_user_sequence_chain(
    nc: NatsClient, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    received: list[Msg] = []

    async def collect(msg: Msg) -> None:
        received.append(msg)

    await nc.subscribe(subjects.live_events("alice"), cb=collect)
    first = await js.publish(subjects.events("alice"), b"1", headers={"Nats-Msg-Id": "a1"})
    await js.publish(subjects.events("bob"), b"x", headers={"Nats-Msg-Id": "b1"})
    second = await js.publish(subjects.events("alice"), b"2", headers={"Nats-Msg-Id": "a2"})
    duplicate = await js.publish(subjects.events("alice"), b"2", headers={"Nats-Msg-Id": "a2"})
    await eventually(lambda: len(received) == 2)
    await asyncio.sleep(0.1)
    assert duplicate.duplicate
    assert len(received) == 2
    headers = [m.headers or {} for m in received]
    assert [h["Nats-Sequence"] for h in headers] == [str(first.seq), str(second.seq)]
    assert [h["Nats-Last-Sequence"] for h in headers] == ["0", str(first.seq)]


async def _bucket(js: JetStreamContext, ttl: float = 1.0) -> LeaseBucket:
    kv = await js.create_key_value(KeyValueConfig(bucket="lease-test", ttl=ttl, history=1))
    info = await js.stream_info("KV_lease-test")
    return LeaseBucket(kv, generation=generation_of(info.created))


async def test_a_lease_has_one_holder_and_survives_renewals(js: JetStreamContext) -> None:
    bucket = await _bucket(js)
    lease = await bucket.acquire("p.1", "engine-a")
    assert lease is not None
    assert await bucket.acquire("p.1", "engine-b") is None
    renewed = await bucket.renew(lease)
    assert renewed.token > lease.token
    assert await bucket.holder("p.1") == "engine-a"
    with pytest.raises(LeaseLost):
        await bucket.renew(lease)  # a stale revision is somebody else's write


async def test_an_abandoned_lease_expires_and_the_token_keeps_growing(js: JetStreamContext) -> None:
    bucket = await _bucket(js, ttl=1.0)
    first = await bucket.acquire("p.2", "engine-a")
    assert first is not None
    taken: list[FencingToken] = []

    async def take_over() -> bool:
        lease = await bucket.acquire("p.2", "engine-b")
        if lease is not None:
            taken.append(lease.token)
        return lease is not None

    await eventually(take_over, within=5.0, interval=0.1)
    assert taken[0] > first.token
    with pytest.raises(LeaseLost):
        await bucket.renew(first)


async def test_releasing_a_lost_lease_does_not_touch_the_new_owner(js: JetStreamContext) -> None:
    bucket = await _bucket(js, ttl=1.0)
    old = await bucket.acquire("p.3", "engine-a")
    assert old is not None
    await bucket.release(old)
    new = await bucket.acquire("p.3", "engine-b")
    assert new is not None
    await bucket.release(old)  # late release from the previous owner: must be a no-op
    assert await bucket.holder("p.3") == "engine-b"
    assert new.token > old.token


async def test_members_are_listed_by_prefix(js: JetStreamContext) -> None:
    bucket = await _bucket(js)
    assert await bucket.keys("m.") == []
    await bucket.heartbeat("m.engine-a", b"{}")
    await bucket.heartbeat("m.engine-b", b"{}")
    await bucket.acquire("p.0", "engine-a")
    assert await bucket.keys("m.") == ["m.engine-a", "m.engine-b"]


def test_generation_of_missing_timestamp_is_zero() -> None:
    assert generation_of(None) == 0
    assert generation_of(datetime(2026, 1, 1, tzinfo=UTC)) == 1_767_225_600


async def _pending(db: AsyncEngine, *events: PendingEvent) -> list[outbox.OutboxRow]:
    async with db.begin() as conn:
        return await outbox.insert(conn, list(events))


async def _count_outbox(db: AsyncEngine) -> int:
    async with db.connect() as conn:
        count: int = (await conn.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
        return count


async def test_fast_path_publishes_and_clears_the_outbox(
    db: AsyncEngine, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    rows = await _pending(
        db,
        PendingEvent(subjects.events("u1"), "e1", b'{"n":1}'),
        PendingEvent(subjects.events("u1"), "e2", b'{"n":2}'),
    )
    relay = OutboxRelay(db, js)
    assert await relay.relay(rows) == 2
    assert await _count_outbox(db) == 0
    info = await js.stream_info(subjects.EVENTS_STREAM)
    assert info.state.messages == 2


async def test_sweeper_recovers_events_left_behind_and_duplicates_are_dropped(
    db: AsyncEngine, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent(subjects.events("u2"), "e3", b"{}"))
    relay = OutboxRelay(db, js, sweep_min_age_s=0.0)
    # simulate a crash after publishing but before deleting: publish, keep the row
    await js.publish(rows[0].subject, rows[0].payload, headers={"Nats-Msg-Id": rows[0].msg_id})
    fresh = await _pending(db, PendingEvent(subjects.events("u2"), "e4", b"{}"))
    assert fresh
    assert await relay.sweep_once() == 2
    assert await _count_outbox(db) == 0
    info = await js.stream_info(subjects.EVENTS_STREAM)
    assert info.state.messages == 2  # e3 once, e4 once


async def test_sweeper_leaves_young_rows_to_the_fast_path(
    db: AsyncEngine, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    await _pending(db, PendingEvent(subjects.events("u3"), "e5", b"{}"))
    relay = OutboxRelay(db, js, sweep_min_age_s=60.0)
    assert await relay.sweep_once() == 0
    assert await _count_outbox(db) == 1
    await relay.refresh_backlog_metrics()


async def test_events_without_a_stream_stay_in_the_outbox(
    db: AsyncEngine, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent("nowhere.subject", "e6", b"{}"))
    relay = OutboxRelay(db, js, publish_timeout_s=0.5)
    assert await relay.relay(rows) == 0
    assert await _count_outbox(db) == 1
