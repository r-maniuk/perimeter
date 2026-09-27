"""Broker topology, leases and the outbox relay against a real NATS server."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import nats
import pytest
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.errors import BadSubjectError, ConnectionClosedError
from nats.js import JetStreamContext
from nats.js.api import KeyValueConfig
from nats.js.errors import NotFoundError
from nats.js.kv import KeyValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.bus.leases import FencingToken, LeaseBucket, LeaseLost, generation_of
from perimeter.bus.publish import Ack, PublishError, StreamPublisher
from perimeter.bus.relay import OutboxRelay
from perimeter.config import NatsSettings
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


async def test_bucket_lifetimes_follow_the_configuration(
    js: JetStreamContext, topo: topology.Topology
) -> None:
    # A revocation must outlive the tokens it revokes, whatever SESSION_TTL_S says today.
    await topology.ensure(js, topo)
    longer = topology.Topology(
        partitions=topo.partitions, lease_ttl_s=3, sessions_ttl_s=5, revoked_ttl_s=90
    )
    await topology.ensure(js, longer)
    revoked = await js.stream_info(f"KV_{subjects.KV_REVOKED}")
    engine = await js.stream_info(f"KV_{subjects.KV_ENGINE}")
    assert revoked.config.max_age == 90
    assert engine.config.max_age == 3


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
    return LeaseBucket(kv)


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


async def test_a_lease_that_expires_while_it_is_being_acquired_is_taken(
    js: JetStreamContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The client's create answers "taken" with a second round trip that reads the key back; hold
    # that read until the holder's entry has expired, which is the window a busy engine can hit.
    kv = await js.create_key_value(KeyValueConfig(bucket="lease-race", ttl=1.0, history=1))
    bucket = LeaseBucket(kv)
    first = await bucket.acquire("p.4", "engine-a")
    assert first is not None
    read_back = kv._get

    async def expired() -> bool:
        try:
            await js.get_last_msg("KV_lease-race", "$KV.lease-race.p.4")
        except NotFoundError:
            return True
        return False

    async def read_back_once_expired(key: str, revision: int | None = None) -> KeyValue.Entry:
        await eventually(expired, within=5.0, interval=0.05)
        return await read_back(key, revision)

    monkeypatch.setattr(kv, "_get", read_back_once_expired)
    second = await bucket.acquire("p.4", "engine-b")
    monkeypatch.undo()
    assert second is not None
    assert second.token > first.token
    assert await bucket.holder("p.4") == "engine-b"


async def test_a_recreated_bucket_raises_the_generation_of_new_tokens(js: JetStreamContext) -> None:
    # A broker whose store was lost gets its buckets back empty, revisions starting from 1, while
    # engines keep running: tokens they take from then on must still beat every older one.
    bucket = await _bucket(js)
    before = await bucket.acquire("p.5", "engine-a")
    assert before is not None
    for _ in range(3):
        before = await bucket.renew(before)
    await js.delete_key_value("lease-test")
    await asyncio.sleep(1.1)  # generations are creation seconds
    await js.create_key_value(KeyValueConfig(bucket="lease-test", ttl=1.0, history=1))
    after = await bucket.acquire("p.5", "engine-b")
    assert after is not None
    assert after.revision < before.revision
    assert after.token > before.token


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


async def test_the_stream_publisher_is_acknowledged_and_sees_duplicates(
    stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    first = await (await stream.publish(subjects.events("u9"), b"{}", {"Nats-Msg-Id": "d1"}))
    again = await (await stream.publish(subjects.events("u9"), b"{}", {"Nats-Msg-Id": "d1"}))
    assert first.stream == subjects.EVENTS_STREAM
    assert not first.duplicate
    assert again.duplicate
    assert again.seq == first.seq
    assert stream.pending == 0


async def test_a_subject_without_a_stream_fails_the_publish(stream: StreamPublisher) -> None:
    with pytest.raises(PublishError, match="no stream"):
        await (await stream.publish("nowhere.at.all", b"{}"))
    assert stream.pending == 0


async def test_messages_that_cannot_be_sent_leave_nothing_behind(
    stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    # The client refuses the subject before anything is sent; each refusal used to cost the
    # JetStream client a pending slot for good, until publishing hung.
    for _ in range(3):
        with pytest.raises(BadSubjectError):
            await stream.publish("tlm.veh-1\n", b"r")
    assert stream.pending == 0
    ack = await (await stream.publish(subjects.telemetry("veh-1"), b"r"))
    assert ack.stream == subjects.TELEMETRY_STREAM


async def test_a_closed_connection_leaves_nothing_behind(
    nats_settings: NatsSettings, provisioned: topology.Topology
) -> None:
    nc = await nats.connect(nats_settings.url)
    stream = StreamPublisher(nc)
    assert (await (await stream.publish(subjects.telemetry("veh-2"), b"r"))).seq > 0
    await nc.close()
    with pytest.raises(ConnectionClosedError):
        await stream.publish(subjects.telemetry("veh-2"), b"r")
    assert stream.pending == 0
    await stream.close()


async def test_a_publish_nobody_waits_for_is_forgotten(
    stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    future = await stream.publish(subjects.telemetry("veh-3"), b"r")
    future.cancel()
    await asyncio.sleep(0)
    assert stream.pending == 0
    await asyncio.sleep(0.2)  # its acknowledgement still arrives, and is ignored
    assert stream.pending == 0


async def _pending(db: AsyncEngine, *events: PendingEvent) -> list[outbox.OutboxRow]:
    async with db.begin() as conn:
        return await outbox.insert(conn, list(events))


async def _count_outbox(db: AsyncEngine) -> int:
    async with db.connect() as conn:
        count: int = (await conn.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
        return count


async def test_fast_path_publishes_and_clears_the_outbox(
    db: AsyncEngine, js: JetStreamContext, stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    rows = await _pending(
        db,
        PendingEvent(subjects.events("u1"), "e1", b'{"n":1}'),
        PendingEvent(subjects.events("u1"), "e2", b'{"n":2}'),
    )
    relay = OutboxRelay(db, stream)
    assert await relay.relay(rows) == 2
    assert await _count_outbox(db) == 0
    info = await js.stream_info(subjects.EVENTS_STREAM)
    assert info.state.messages == 2


async def test_sweeper_recovers_events_left_behind_and_duplicates_are_dropped(
    db: AsyncEngine, js: JetStreamContext, stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent(subjects.events("u2"), "e3", b"{}"))
    relay = OutboxRelay(db, stream, sweep_min_age_s=0.0)
    # simulate a crash after publishing but before deleting: publish, keep the row
    await js.publish(rows[0].subject, rows[0].payload, headers={"Nats-Msg-Id": rows[0].msg_id})
    fresh = await _pending(db, PendingEvent(subjects.events("u2"), "e4", b"{}"))
    assert fresh
    assert await relay.sweep_once() == 2
    assert await _count_outbox(db) == 0
    info = await js.stream_info(subjects.EVENTS_STREAM)
    assert info.state.messages == 2  # e3 once, e4 once


async def test_without_tracing_events_keep_and_carry_no_trace_context(
    db: AsyncEngine, js: JetStreamContext, stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent(subjects.events("u6"), "e9", b"{}"))
    assert rows[0].trace_context is None
    assert await OutboxRelay(db, stream, sweep_min_age_s=0.0).sweep_once() == 1
    stored = await js.get_last_msg(subjects.EVENTS_STREAM, subjects.events("u6"))
    assert stored.headers == {"Nats-Msg-Id": "e9"}


async def test_sweeper_leaves_young_rows_to_the_fast_path(
    db: AsyncEngine, stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    await _pending(db, PendingEvent(subjects.events("u3"), "e5", b"{}"))
    relay = OutboxRelay(db, stream, sweep_min_age_s=60.0)
    assert await relay.sweep_once() == 0
    assert await _count_outbox(db) == 1
    await relay.refresh_backlog_metrics()


async def test_a_claimed_row_waits_until_its_hold_lapses(
    db: AsyncEngine, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent(subjects.events("u4"), "e7", b"{}"))
    async with db.begin() as conn:
        first = await outbox.claim_stale(conn, min_age_s=0, limit=10, hold_s=0)
    async with db.begin() as conn:
        again = await outbox.claim_stale(conn, min_age_s=0, limit=10, hold_s=60)
    async with db.begin() as conn:
        held = await outbox.claim_stale(conn, min_age_s=0, limit=10, hold_s=60)
    assert [r.id for r in first] == [rows[0].id]
    assert [r.id for r in again] == [rows[0].id]  # the first hold lapsed at once
    assert held == []  # the second one has not


class SlowStream:
    """Acknowledges only once ``answer`` is set, like a broker that is slow to reply."""

    def __init__(self) -> None:
        self.answer = asyncio.Event()
        self._replies: list[asyncio.Task[None]] = []

    async def publish(
        self, subject: str, payload: bytes, headers: object = None
    ) -> asyncio.Future[Ack]:
        future: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()

        async def reply() -> None:
            await self.answer.wait()
            future.set_result(Ack(subjects.EVENTS_STREAM, 1))

        self._replies.append(asyncio.create_task(reply()))
        return future


async def test_the_sweeper_holds_no_lock_while_the_broker_answers(
    db: AsyncEngine, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent(subjects.events("u5"), "e8", b"{}"))
    stream = SlowStream()
    relay = OutboxRelay(db, stream, sweep_min_age_s=0.0)  # type: ignore[arg-type]
    sweeping = asyncio.create_task(relay.sweep_once())

    async def claimed() -> bool:
        async with db.connect() as conn:
            query = text("SELECT claimed_until IS NOT NULL FROM outbox WHERE id = :id")
            return bool((await conn.execute(query, {"id": rows[0].id})).scalar_one())

    await eventually(claimed)
    # the fast path deleting the same row meanwhile must not wait for the sweeper
    async with db.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '200ms'"))
        await outbox.delete(conn, [rows[0].id])
    stream.answer.set()
    assert await sweeping == 1
    assert await _count_outbox(db) == 0


async def test_events_without_a_stream_stay_in_the_outbox(
    db: AsyncEngine, stream: StreamPublisher, provisioned: topology.Topology
) -> None:
    rows = await _pending(db, PendingEvent("nowhere.subject", "e6", b"{}"))
    relay = OutboxRelay(db, stream, publish_timeout_s=0.5)
    assert await relay.relay(rows) == 0
    assert await _count_outbox(db) == 1
