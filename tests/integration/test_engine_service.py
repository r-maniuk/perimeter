"""Engine services in-process against real PostGIS and NATS: the whole path, failover, heartbeat."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

import pytest
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.bus.leases import FencingToken, Lease
from perimeter.config import (
    EngineSettings,
    LiveSettings,
    Settings,
    TelemetrySettings,
    load_settings,
)
from perimeter.domain import tiles
from perimeter.domain.reports import TelemetryRecord
from perimeter.engine.batch import FencedOut
from perimeter.engine.service import EngineService, StartupError
from perimeter.engine.worker import (
    LeaseHandle,
    PartitionSubscription,
    PartitionWorker,
    WorkerExit,
    unacknowledged,
)
from perimeter.wire import subjects, telemetry
from perimeter.wire.frames import FrameKind, decode_tile
from tests.support import eventually

T0 = 1_790_000_000_000
DAM = (4.8926, 52.3731)  # lon, lat
AWAY = (4.9226, 52.3731)
LEASE_TTL_S = 2.0
ENGINE_KEYS = {
    "service",
    "instance",
    "ts",
    "loop_lag_p99_ms",
    "partitions",
    "reports_rate",
    "batches_rate",
    "batch_p50_ms",
    "batch_p99_ms",
    "commit_lag_p99_ms",
    "alerts_rate",
    "late_rate",
    "relay_backlog",
}

Start = Callable[..., Awaitable[EngineService]]


@pytest.fixture
def topo() -> topology.Topology:
    """Four partitions and 2-second leases; a short ack wait so crash recovery is quick to see."""
    return topology.Topology(
        partitions=4,
        lease_ttl_s=LEASE_TTL_S,
        sessions_ttl_s=5,
        revoked_ttl_s=60,
        consumer_ack_wait_s=3,
    )


def tuned(settings: Settings, **groups: object) -> Settings:
    base: dict[str, object] = {
        "database": settings.database,
        "nats": settings.nats,
        "security": settings.security,
        "telemetry": settings.telemetry,
        "ingest": settings.ingest,
        "engine": EngineSettings(
            lease_ttl_s=settings.engine.lease_ttl_s, fetch_wait_s=0.2, batch_max=500
        ),
        "live": LiveSettings(tile_flush_ms=50),
        "observability": settings.observability,
    }
    return load_settings(**(base | groups))


@pytest.fixture
async def engines(
    settings: Settings, provisioned: topology.Topology, db: AsyncEngine
) -> AsyncIterator[Start]:
    running: list[EngineService] = []

    async def start(instance: str, **groups: object) -> EngineService:
        service = EngineService(tuned(settings, **groups), instance=instance)
        await service.start()
        running.append(service)
        return service

    yield start
    for service in running:
        await service.stop()


class Inbox:
    """Collects core NATS messages per subject."""

    def __init__(self, nc: NatsClient) -> None:
        self.nc = nc
        self.messages: dict[str, list[Msg]] = defaultdict(list)

    async def listen(self, *subject_names: str) -> None:
        for name in subject_names:

            async def collect(msg: Msg, name: str = name) -> None:
                self.messages[name].append(msg)

            await self.nc.subscribe(name, cb=collect)
        await self.nc.flush()


async def make_user(db: AsyncEngine) -> uuid.UUID:
    async with db.begin() as conn:
        user: uuid.UUID = (
            await conn.execute(text("INSERT INTO users (username) VALUES ('alice') RETURNING id"))
        ).scalar_one()
    return user


async def make_zone(db: AsyncEngine, owner: uuid.UUID) -> uuid.UUID:
    async with db.begin() as conn:
        zone: uuid.UUID = (
            await conn.execute(
                text(
                    """
                    INSERT INTO geozones (owner_id, name, center, radius_m)
                    VALUES (:owner, 'Dam Square',
                            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 250)
                    RETURNING id
                    """
                ),
                {"owner": owner, "lon": DAM[0], "lat": DAM[1]},
            )
        ).scalar_one()
    return zone


async def publish(js: JetStreamContext, records: Sequence[TelemetryRecord]) -> None:
    """Publish like the api does: to ``tlm.<device>``, de-duplicated by device and event time."""
    acks = [
        await js.publish_async(
            subjects.telemetry(r.device_id),
            telemetry.encode(r),
            headers={"Nats-Msg-Id": telemetry.dedup_id(r)},
        )
        for r in records
    ]
    await asyncio.gather(*acks)


async def positions(db: AsyncEngine) -> dict[str, int]:
    async with db.connect() as conn:
        rows = await conn.execute(
            text("SELECT device_id, (extract(epoch FROM recorded_at) * 1000)::bigint FROM devices")
        )
        return dict(rows.all())


async def alerts(db: AsyncEngine) -> list[tuple[str, str, int]]:
    async with db.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT device_id, kind, (extract(epoch FROM occurred_at) * 1000)::bigint
                FROM alerts ORDER BY device_id, occurred_at
                """
            )
        )
        return [(device, kind, at) for device, kind, at in rows]


def owns_all(*services: EngineService, partitions: int = 4) -> bool:
    owned = [p for service in services for p in service.partitions]
    return sorted(owned) == list(range(partitions))


def zigzag(devices: int, steps: range) -> list[TelemetryRecord]:
    """Every device alternates between outside (even steps) and inside (odd steps) the zone."""
    return [
        TelemetryRecord(
            f"veh-{d:03d}",
            T0 + step * 1_000,
            T0,
            (DAM if step % 2 else AWAY)[1],
            (DAM if step % 2 else AWAY)[0],
        )
        for step in steps
        for d in range(devices)
    ]


def expected_alerts(devices: int, steps: int) -> list[tuple[str, str, int]]:
    return [
        (f"veh-{d:03d}", "enter" if step % 2 else "exit", T0 + step * 1_000)
        for d in range(devices)
        for step in range(1, steps)
    ]


async def test_a_report_reaches_the_database_and_every_live_channel(
    engines: Start, db: AsyncEngine, nc: NatsClient, js: JetStreamContext
) -> None:
    engine = await engines("engine-a")
    await eventually(lambda: owns_all(engine), within=10)
    owner = await make_user(db)
    zone = await make_zone(db, owner)
    tile = tiles.quadkey_for(DAM[0], DAM[1], engine.settings.live.tile_zoom)
    inbox = Inbox(nc)
    channels = [subjects.live_events(owner), tiles.subject_for(tile), subjects.live_pulses(owner)]
    await inbox.listen(*channels)

    record = TelemetryRecord("veh-1", T0, T0 - 20, DAM[1], DAM[0], speed=12.5, heading=90.0)
    await publish(js, [record])
    await eventually(lambda: all(inbox.messages[c] for c in channels), within=10)

    event, frame, pulse = (inbox.messages[c][0] for c in channels)
    alert = json.loads(event.data)
    assert alert["type"] == "alert"
    assert alert["data"]["kind"] == "enter"
    assert alert["data"]["zone"] == {"id": str(zone), "name": "Dam Square"}
    assert event.headers is not None
    assert event.headers["Nats-Sequence"] == "1"

    decoded = decode_tile(frame.data)
    assert decoded.kind is FrameKind.LIVE
    assert (decoded.x, decoded.y, decoded.zoom) == tuple(tiles.tile_of_quadkey(tile)[:3])
    (point,) = decoded.points
    assert (point.device_id, point.recorded_at_ms, point.speed_mps) == ("veh-1", T0, 12.5)
    assert abs(point.lat - DAM[1]) < 1e-7

    assert json.loads(pulse.data) == {
        "type": "pulse",
        "window_ms": 50,
        "zones": {str(zone): ["veh-1"]},
    }
    assert await positions(db) == {"veh-1": T0}
    assert await alerts(db) == [("veh-1", "enter", T0)]


async def test_two_engines_split_the_partitions_and_hand_over_without_losing_a_report(
    engines: Start, db: AsyncEngine, js: JetStreamContext
) -> None:
    a = await engines("engine-a")
    b = await engines("engine-b")
    await eventually(lambda: owns_all(a, b) and a.partitions and b.partitions, within=10)
    assert set(a.partitions).isdisjoint(b.partitions)
    owner = await make_user(db)
    await make_zone(db, owner)
    devices, steps = 40, 6

    await publish(js, zigzag(devices, range(3)))
    await a.stop()  # while its share of those reports may still be in flight
    loop = asyncio.get_running_loop()
    stopped_at = loop.time()
    await eventually(lambda: owns_all(b), within=LEASE_TTL_S + 1, interval=0.01)
    handover_s = loop.time() - stopped_at
    await publish(js, zigzag(devices, range(3, steps)))

    last = T0 + (steps - 1) * 1_000
    await eventually(lambda: _all_at(db, devices, last), within=15, interval=0.1)
    assert await alerts(db) == expected_alerts(devices, steps)  # nothing lost, nothing twice
    events = await js.stream_info(subjects.EVENTS_STREAM)
    assert events.state.messages == devices * (steps - 1)
    assert handover_s < LEASE_TTL_S, "a graceful stop should not make peers wait for the TTL"


async def _all_at(db: AsyncEngine, devices: int, at: int) -> bool:
    current = await positions(db)
    return len(current) == devices and set(current.values()) == {at}


async def test_a_crashed_engine_is_replaced_once_its_leases_expire(
    engines: Start, db: AsyncEngine, js: JetStreamContext
) -> None:
    a = await engines("engine-a")
    b = await engines("engine-b")
    await eventually(lambda: owns_all(a, b) and a.partitions and b.partitions, within=10)
    owner = await make_user(db)
    await make_zone(db, owner)
    devices = 30
    await publish(js, zigzag(devices, range(2)))
    await a.abort()  # no drain, no release: as if the process had been killed
    loop = asyncio.get_running_loop()
    crashed_at = loop.time()
    await eventually(lambda: owns_all(b), within=LEASE_TTL_S * 3, interval=0.02)
    takeover_s = loop.time() - crashed_at
    await publish(js, zigzag(devices, range(2, 4)))
    await eventually(lambda: _all_at(db, devices, T0 + 3_000), within=15, interval=0.1)

    assert await alerts(db) == expected_alerts(devices, 4)  # nothing lost, nothing twice
    events = await js.stream_info(subjects.EVENTS_STREAM)
    assert events.state.messages == len(expected_alerts(devices, 4))
    assert takeover_s <= LEASE_TTL_S + LEASE_TTL_S / 3 + 0.5


async def partition_of(js: JetStreamContext, device: str) -> int:
    info = await js.stream_info(subjects.TELEMETRY_STREAM, subjects_filter=">")
    stored = next(s for s in (info.state.subjects or {}) if subjects.device_of(s) == device)
    return subjects.partition_of(stored)


async def test_a_new_owner_applies_what_a_crashed_owner_left_unacknowledged_first(
    engines: Start, db: AsyncEngine, js: JetStreamContext
) -> None:
    owner = await make_user(db)
    await make_zone(db, owner)
    await publish(js, zigzag(1, range(3)))  # outside, inside (enter), outside (exit)
    partition = await partition_of(js, "veh-000")
    crashed = await js.pull_subscribe_bind(
        durable=subjects.engine_consumer(partition), stream=subjects.TELEMETRY_STREAM
    )
    assert len(await crashed.fetch(10, timeout=2)) == 3  # fetched, then died before acking
    await publish(js, zigzag(1, range(3, 5)))  # newer: inside (enter), outside (exit)

    await engines("engine-a")
    await eventually(lambda: _all_at(db, 1, T0 + 4_000), within=10)
    assert await alerts(db) == expected_alerts(1, 5)  # the crashed batch was not lost as late
    await asyncio.sleep(3.5)  # past the ack wait: the broker redelivers the crashed batch...
    assert await alerts(db) == expected_alerts(1, 5)  # ...and it changes nothing
    info = await js.consumer_info(subjects.TELEMETRY_STREAM, subjects.engine_consumer(partition))
    assert info.num_ack_pending == 0


async def test_the_unacknowledged_range_of_a_partition_is_read_back_in_order(
    js: JetStreamContext, provisioned: topology.Topology
) -> None:
    await publish(js, [TelemetryRecord(f"veh-{i % 5}", T0 + i, T0, 52.37, 4.9) for i in range(20)])
    partition = await partition_of(js, "veh-0")
    consumer = await js.pull_subscribe_bind(
        durable=subjects.engine_consumer(partition), stream=subjects.TELEMETRY_STREAM
    )
    delivered = await consumer.fetch(100, timeout=2)
    assert all(subjects.partition_of(m.subject) == partition for m in delivered)
    await delivered[0].ack_sync()
    await delivered[1].ack_sync()
    left = await unacknowledged(js, partition)
    assert left == [(m.subject, m.data) for m in delivered[2:]]
    others = [p for p in range(provisioned.partitions) if p != partition]
    assert all([await unacknowledged(js, p) == [] for p in others])
    for m in delivered[2:]:
        await m.ack_sync()
    assert await unacknowledged(js, partition) == []


async def test_a_fenced_worker_hands_back_what_it_holds_and_takes_nothing_more(
    nc: NatsClient, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    # A zombie woken after a takeover. While it applied its last batch, a status answer to one of
    # its pull requests and one more report reached its inbox: it hands that report back, and must
    # not ask the broker for the reports behind it, which the partition's new owner is to fetch.
    reports = zigzag(1, range(4))
    await publish(js, reports[:1])
    partition = await partition_of(js, "veh-000")
    consumer = subjects.engine_consumer(partition)
    zombie = await PartitionSubscription.bind(nc, partition)
    arrivals = await nc.subscribe(zombie.inbox)  # sees whatever reaches the zombie's inbox

    async def pull() -> None:  # answered after the zombie's fetch stopped waiting for it
        await nc.publish(
            f"$JS.API.CONSUMER.MSG.NEXT.{subjects.TELEMETRY_STREAM}.{consumer}",
            b'{"batch": 1, "no_wait": true}',
            reply=zombie.inbox,
        )

    async def arrived(matches: Callable[[Msg], bool]) -> None:
        while not matches(await arrivals.next_msg(timeout=5)):
            pass

    class Fenced:
        async def apply(
            self,
            partition: int,
            token: FencingToken,
            records: Sequence[TelemetryRecord],
            *,
            trace: Mapping[str, str] | None = None,
        ) -> object:
            await pull()  # nothing is left to deliver, so the broker answers with a status
            await arrived(lambda msg: bool(msg.headers and "Status" in msg.headers))
            await publish(js, reports[1:2])
            await pull()  # this time a report arrives
            await arrived(lambda msg: msg.data == telemetry.encode(reports[1]))
            await publish(js, reports[2:])  # the reports the new owner is about to fetch
            await nc.flush()  # the zombie's inbox has received all the spy has
            raise FencedOut("a newer owner has committed")

    async def subscribe() -> PartitionSubscription:
        return zombie

    async def nothing_left() -> list[tuple[str, bytes]]:
        return []

    worker = PartitionWorker(
        partition,
        subscribe=subscribe,
        in_flight=nothing_left,
        processor=Fenced(),
        lease=LeaseHandle(Lease(f"p.{partition}", "engine-z", 1, 1), valid_until=math.inf),
        batch_max=1,
        fetch_wait_s=1.0,
    )
    assert await asyncio.wait_for(worker.run(), 15) is WorkerExit.FENCED
    await arrivals.unsubscribe()
    info = await js.consumer_info(subjects.TELEMETRY_STREAM, consumer)
    assert info.num_pending == 2, "the reports behind what the zombie held were never delivered"
    successor = await js.pull_subscribe_bind(durable=consumer, stream=subjects.TELEMETRY_STREAM)
    delivered = await successor.fetch(len(reports), timeout=2)  # well within the 3 s ack wait
    assert {
        telemetry.decode(m.data).recorded_at_ms - T0: m.metadata.num_delivered for m in delivered
    } == {0: 2, 1_000: 2, 2_000: 1, 3_000: 1}  # both handed back at once, the rest new


async def test_the_heartbeat_describes_the_engine(engines: Start, nc: NatsClient) -> None:
    engine = await engines("engine-a")
    await eventually(lambda: owns_all(engine), within=10)
    inbox = Inbox(nc)
    subject = subjects.metrics_heartbeat("engine", "engine-a")
    await inbox.listen(subject)
    await eventually(lambda: inbox.messages[subject], within=5)
    beat = json.loads(inbox.messages[subject][-1].data)
    assert set(beat) == ENGINE_KEYS
    assert (beat["service"], beat["instance"]) == ("engine", "engine-a")
    assert beat["partitions"] == [0, 1, 2, 3]


async def test_messages_that_can_never_apply_are_terminated_not_redelivered(
    engines: Start, db: AsyncEngine, js: JetStreamContext
) -> None:
    engine = await engines("engine-a")
    await eventually(lambda: owns_all(engine), within=10)
    before = REGISTRY.get_sample_value("perimeter_engine_poison_total", {"reason": "undecodable"})
    ack = await js.publish(subjects.telemetry("veh-9"), b"\xc1 definitely not msgpack")
    await publish(js, [TelemetryRecord("veh-9", T0, T0, DAM[1], DAM[0])])
    await eventually(lambda: _position_is(db, "veh-9", T0), within=10)
    stored = await js.get_msg(subjects.TELEMETRY_STREAM, ack.seq)
    consumer = subjects.engine_consumer(subjects.partition_of(stored.subject or ""))
    info = await js.consumer_info(subjects.TELEMETRY_STREAM, consumer)
    assert info.num_ack_pending == 0
    assert info.num_redelivered == 0
    after = REGISTRY.get_sample_value("perimeter_engine_poison_total", {"reason": "undecodable"})
    assert (after or 0) == (before or 0) + 1


async def _position_is(db: AsyncEngine, device: str, at: int) -> bool:
    return (await positions(db)).get(device) == at


async def test_an_engine_refuses_a_broker_partitioned_differently(
    settings: Settings, provisioned: topology.Topology
) -> None:
    service = EngineService(
        tuned(settings, telemetry=TelemetrySettings(partitions=provisioned.partitions * 2)),
        instance="engine-x",
    )
    with pytest.raises(topology.TopologyError, match="partitions"):
        await service.start()
    assert service.partitions == []


async def test_an_engine_refuses_a_database_without_the_schema(
    settings: Settings, provisioned: topology.Topology
) -> None:
    empty = settings.database.model_copy(update={"name": "postgres"})
    service = EngineService(tuned(settings, database=empty), instance="engine-x")
    with pytest.raises(StartupError, match="init job"):
        await service.start()
