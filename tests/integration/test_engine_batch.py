"""The batch pipeline on real PostGIS and JetStream: alerts, redelivery, fencing, zone races."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

import pytest
import sqlalchemy.exc
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from perimeter.bus import topology
from perimeter.bus.leases import FencingToken
from perimeter.bus.relay import OutboxRelay
from perimeter.domain.presence import ZoneRules
from perimeter.domain.reports import TelemetryRecord
from perimeter.engine import sql
from perimeter.engine.batch import BatchProcessor, FencedOut
from perimeter.engine.metrics import EngineStats
from perimeter.storage.outbox import OutboxRow
from perimeter.wire import subjects
from tests.support import eventually

T0 = 1_790_000_000_000
DAM = (4.8926, 52.3731)  # lon, lat
AWAY = (4.9226, 52.3731)  # about 2 km east
PARTITION = 3
TOKEN = FencingToken(generation=1_790_000_000, revision=1)
NEWER = TOKEN._replace(revision=11)  # a successor's lease
OLDER = TOKEN._replace(revision=6)  # a zombie's lease, renewed before it paused


class LiveRecorder:
    """Stands in for the tile publisher: remembers what the batches handed over."""

    def __init__(self) -> None:
        self.moved: list[TelemetryRecord] = []
        self.occupancy: list[dict[uuid.UUID, dict[uuid.UUID, list[str]]]] = []

    def positions(self, records: Iterable[TelemetryRecord]) -> None:
        self.moved.extend(records)

    def pulses(self, pulses: Mapping[uuid.UUID, Mapping[uuid.UUID, Iterable[str]]]) -> None:
        self.occupancy.append(
            {owner: {z: sorted(d) for z, d in zones.items()} for owner, zones in pulses.items()}
        )


class UnreachableRelay(OutboxRelay):
    """A fast path that could not reach the broker right after the commit."""

    async def relay(self, rows: Sequence[OutboxRow]) -> int:
        return 0


@pytest.fixture
def live() -> LiveRecorder:
    return LiveRecorder()


@pytest.fixture
def processor(
    db: AsyncEngine, js: JetStreamContext, provisioned: topology.Topology, live: LiveRecorder
) -> BatchProcessor:
    return BatchProcessor(db, OutboxRelay(db, js), live, owner="engine-test", stats=EngineStats())


async def make_user(db: AsyncEngine, name: str = "alice") -> uuid.UUID:
    async with db.begin() as conn:
        user: uuid.UUID = (
            await conn.execute(
                text("INSERT INTO users (username) VALUES (:u) RETURNING id"), {"u": name}
            )
        ).scalar_one()
    return user


async def make_zone(
    db: AsyncEngine,
    owner: uuid.UUID,
    *,
    where: tuple[float, float] = DAM,
    radius_m: float = 250,
    name: str = "Dam Square",
    **flags: Any,
) -> uuid.UUID:
    columns = {"notify_enter": True, "notify_exit": True, "dwell_s": None, "is_active": True}
    columns.update(flags)
    async with db.begin() as conn:
        zone: uuid.UUID = (
            await conn.execute(
                text(
                    """
                    INSERT INTO geozones (owner_id, name, center, radius_m, is_active,
                                          notify_enter, notify_exit, dwell_s)
                    VALUES (:owner, :name, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                            :radius, :is_active, :notify_enter, :notify_exit, :dwell_s)
                    RETURNING id
                    """
                ),
                {"owner": owner, "name": name, "lon": where[0], "lat": where[1], "radius": radius_m}
                | columns,
            )
        ).scalar_one()
    return zone


def report(
    at: int, where: tuple[float, float] = DAM, *, device: str = "veh-1", **extra: float
) -> TelemetryRecord:
    return TelemetryRecord(device, at, at + 40, where[1], where[0], **extra)


async def alerts(db: AsyncEngine) -> list[tuple[str, str, int]]:
    async with db.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT device_id, kind, (extract(epoch FROM occurred_at) * 1000)::bigint
                FROM alerts ORDER BY occurred_at, device_id, kind
                """
            )
        )
        return [(device, kind, at) for device, kind, at in rows]


async def presence(db: AsyncEngine) -> list[tuple[str, uuid.UUID, int, bool]]:
    async with db.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT device_id, zone_id, (extract(epoch FROM entered_at) * 1000)::bigint,
                       dwell_alerted
                FROM zone_presence ORDER BY device_id, zone_id
                """
            )
        )
        return [(device, zone, entered, alerted) for device, zone, entered, alerted in rows]


async def outbox_rows(db: AsyncEngine) -> int:
    async with db.connect() as conn:
        count: int = (await conn.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
        return count


async def last_recorded(db: AsyncEngine, *devices: str) -> dict[str, int]:
    async with db.connect() as conn:
        return await sql.last_recorded(conn, list(devices))


async def test_a_report_inside_a_zone_raises_an_alert_and_publishes_its_event(
    processor: BatchProcessor, db: AsyncEngine, nc: NatsClient, live: LiveRecorder
) -> None:
    owner = await make_user(db)
    zone = await make_zone(db, owner)
    events: list[Msg] = []

    async def collect(msg: Msg) -> None:
        events.append(msg)

    await nc.subscribe(subjects.live_events(owner), cb=collect)
    applied = await processor.apply(PARTITION, TOKEN, [report(T0, speed=4.5)])
    assert (applied.reports, applied.accepted, applied.late, applied.alerts) == (1, 1, 0, 1)

    assert await alerts(db) == [("veh-1", "enter", T0)]
    assert await presence(db) == [("veh-1", zone, T0, False)]
    assert await last_recorded(db, "veh-1") == {"veh-1": T0}
    assert await outbox_rows(db) == 0, "the fast path should have relayed and cleared it"

    await eventually(lambda: events)
    event = json.loads(events[0].data)
    assert event["type"] == "alert"
    assert event["data"]["kind"] == "enter"
    assert event["data"]["device_id"] == "veh-1"
    assert event["data"]["zone"] == {"id": str(zone), "name": "Dam Square"}
    assert event["data"]["position"] == {"lat": DAM[1], "lon": DAM[0]}
    assert event["id"] == event["data"]["alert_id"]
    assert events[0].headers is not None
    assert events[0].headers["Nats-Last-Sequence"] == "0"

    assert [(r.device_id, r.recorded_at_ms) for r in live.moved] == [("veh-1", T0)]
    assert live.occupancy == [{owner: {zone: ["veh-1"]}}]


async def test_a_device_crossing_a_zone_inside_one_batch_enters_and_exits(
    processor: BatchProcessor, db: AsyncEngine
) -> None:
    await make_zone(db, await make_user(db))
    batch = [report(T0 + 2_000, AWAY), report(T0, AWAY), report(T0 + 1_000, DAM)]
    applied = await processor.apply(PARTITION, TOKEN, batch)
    assert applied.alerts == 2
    assert await alerts(db) == [("veh-1", "enter", T0 + 1_000), ("veh-1", "exit", T0 + 2_000)]
    assert await presence(db) == []
    assert await last_recorded(db, "veh-1") == {"veh-1": T0 + 2_000}


async def test_redelivered_duplicate_and_late_reports_raise_nothing_twice(
    processor: BatchProcessor, db: AsyncEngine, live: LiveRecorder
) -> None:
    await make_zone(db, await make_user(db))
    first = [report(T0 - 1_000, AWAY), report(T0, DAM)]
    await processor.apply(PARTITION, TOKEN, first)
    replay = await processor.apply(PARTITION, TOKEN, first)  # redelivered after a lost ack
    assert (replay.accepted, replay.late, replay.alerts) == (0, 2, 0)
    duplicates = await processor.apply(PARTITION, TOKEN, [report(T0 + 1, DAM)] * 3)
    assert (duplicates.accepted, duplicates.late, duplicates.alerts) == (1, 2, 0)
    older = await processor.apply(PARTITION, TOKEN, [report(T0 - 500, AWAY)])  # out of order
    assert (older.accepted, older.late, older.alerts) == (0, 1, 0)
    assert await alerts(db) == [("veh-1", "enter", T0)]
    assert await last_recorded(db, "veh-1") == {"veh-1": T0 + 1}
    assert [r.recorded_at_ms for r in live.moved] == [T0, T0 + 1]


async def test_a_dwell_alert_fires_once_per_stay(
    processor: BatchProcessor, db: AsyncEngine
) -> None:
    await make_zone(db, await make_user(db), dwell_s=10)
    seconds = [0, 5, 10, 20]
    for second in seconds:  # one report per batch, as a slow device would arrive
        await processor.apply(PARTITION, TOKEN, [report(T0 + second * 1_000)])
    await processor.apply(
        PARTITION, TOKEN, [report(T0 + 30_000, AWAY), report(T0 + 40_000), report(T0 + 55_000)]
    )
    assert [(kind, at - T0) for _, kind, at in await alerts(db)] == [
        ("enter", 0),
        ("dwell", 10_000),
        ("exit", 30_000),
        ("enter", 40_000),
        ("dwell", 55_000),
    ]
    assert [alerted for *_, alerted in await presence(db)] == [True]


async def test_notification_flags_silence_alerts_but_presence_and_pulses_remain(
    processor: BatchProcessor, db: AsyncEngine, live: LiveRecorder
) -> None:
    owner = await make_user(db)
    zone = await make_zone(db, owner, notify_enter=False, notify_exit=False)
    entered = await processor.apply(PARTITION, TOKEN, [report(T0)])
    assert entered.alerts == 0
    assert [(device, z) for device, z, *_ in await presence(db)] == [("veh-1", zone)]
    assert live.occupancy == [{owner: {zone: ["veh-1"]}}]
    await processor.apply(PARTITION, TOKEN, [report(T0 + 1_000, AWAY)])
    assert await presence(db) == []
    assert await alerts(db) == []
    assert await outbox_rows(db) == 0


async def test_a_deactivated_zone_forgets_its_occupants_without_alerts(
    processor: BatchProcessor, db: AsyncEngine
) -> None:
    zone = await make_zone(db, await make_user(db))
    await processor.apply(PARTITION, TOKEN, [report(T0)])
    async with db.begin() as conn:
        await conn.execute(text("UPDATE geozones SET is_active = false WHERE id = :z"), {"z": zone})
    applied = await processor.apply(PARTITION, TOKEN, [report(T0 + 1_000)])
    assert applied.alerts == 0
    assert await presence(db) == []
    assert await alerts(db) == [("veh-1", "enter", T0)]


async def test_a_zone_deleted_before_the_batch_is_skipped_and_the_batch_commits(
    processor: BatchProcessor, db: AsyncEngine
) -> None:
    owner = await make_user(db)
    doomed = await make_zone(db, owner, name="doomed")
    await processor.apply(PARTITION, TOKEN, [report(T0)])  # inside: presence row + enter alert
    async with db.begin() as conn:
        await conn.execute(text("DELETE FROM geozones WHERE id = :z"), {"z": doomed})
    applied = await processor.apply(PARTITION, TOKEN, [report(T0 + 1_000), report(T0 + 2_000)])
    assert (applied.accepted, applied.alerts) == (2, 0)
    assert await presence(db) == []
    assert await last_recorded(db, "veh-1") == {"veh-1": T0 + 2_000}
    async with db.connect() as conn:
        kept = (await conn.execute(text("SELECT zone_id, zone_name FROM alerts"))).all()
    assert [tuple(row) for row in kept] == [(None, "doomed")]  # history survives the zone


async def test_a_zone_cannot_be_deleted_under_a_running_batch(
    processor: BatchProcessor, db: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    zone = await make_zone(db, await make_user(db))
    original = sql.zone_rules
    raced: list[str] = []

    async def zone_rules_then_race(
        conn: AsyncConnection, zone_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ZoneRules]:
        rules = await original(conn, zone_ids)
        try:
            async with db.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                await other.execute(text("DELETE FROM geozones WHERE id = :z"), {"z": zone})
        except sqlalchemy.exc.DBAPIError as exc:
            raced.append(str(getattr(exc.orig, "sqlstate", "")))
        return rules

    monkeypatch.setattr(sql, "zone_rules", zone_rules_then_race)
    applied = await processor.apply(PARTITION, TOKEN, [report(T0)])
    assert raced == ["55P03"], "the concurrent delete should have waited for the batch"
    assert applied.alerts == 1
    assert [z for _, z, *_ in await presence(db)] == [zone]
    async with db.begin() as conn:  # once the batch has committed, the delete goes through
        await conn.execute(text("DELETE FROM geozones WHERE id = :z"), {"z": zone})
    assert await presence(db) == []


async def test_an_older_fencing_token_cannot_commit(
    processor: BatchProcessor, db: AsyncEngine, js: JetStreamContext, live: LiveRecorder
) -> None:
    await make_zone(db, await make_user(db))
    successor = BatchProcessor(
        db, OutboxRelay(db, js), LiveRecorder(), owner="engine-new", stats=EngineStats()
    )
    await processor.apply(PARTITION, TOKEN, [report(T0, AWAY)])
    await successor.apply(PARTITION, NEWER, [report(T0 + 1_000, AWAY)])
    with pytest.raises(FencedOut):
        await processor.apply(PARTITION, OLDER, [report(T0 + 2_000, DAM)])
    assert await last_recorded(db, "veh-1") == {"veh-1": T0 + 1_000}
    assert await alerts(db) == [], "a fenced batch must not write anything"
    assert [r.recorded_at_ms for r in live.moved] == [T0]
    await successor.apply(PARTITION, NEWER, [report(T0 + 3_000, DAM)])  # same token: fine
    await processor.apply(PARTITION + 1, TOKEN, [report(T0, DAM, device="veh-2")])  # its own
    async with db.connect() as conn:
        epochs = (
            await conn.execute(
                text("SELECT partition, generation, revision, owner FROM partition_epochs")
            )
        ).all()
    assert sorted(tuple(row) for row in epochs) == [
        (PARTITION, *NEWER, "engine-new"),
        (PARTITION + 1, *TOKEN, "engine-test"),
    ]


@pytest.mark.parametrize(
    "at",
    [
        T0 + 1,
        T0 + 123,
        T0 + 999,
        946_684_800_001,  # 2000-01-01T00:00:00.001Z
        4_102_444_799_999,  # 2099-12-31T23:59:59.999Z
    ],
)
async def test_event_times_survive_the_database_to_the_millisecond(
    processor: BatchProcessor, db: AsyncEngine, at: int
) -> None:
    await processor.apply(PARTITION, TOKEN, [report(at)])
    assert await last_recorded(db, "veh-1") == {"veh-1": at}
    again = await processor.apply(PARTITION, TOKEN, [report(at)])
    assert again.late == 1
    later = await processor.apply(PARTITION, TOKEN, [report(at + 1)])
    assert later.accepted == 1


async def test_many_devices_in_one_batch_are_applied_set_based(
    processor: BatchProcessor, db: AsyncEngine, live: LiveRecorder
) -> None:
    owner = await make_user(db)
    zone = await make_zone(db, owner, radius_m=1_000)
    batch = [
        report(T0 + i, DAM if i % 2 else AWAY, device=f"dev-{i:04d}", speed=float(i % 30))
        for i in range(500)
    ]
    applied = await processor.apply(PARTITION, TOKEN, batch)
    assert (applied.accepted, applied.alerts, applied.moved) == (500, 250, 500)
    assert len(await presence(db)) == 250
    assert len(live.occupancy[0][owner][zone]) == 250


async def test_the_hot_path_reads_are_index_scans(db: AsyncEngine) -> None:
    owner = await make_user(db)
    async with db.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO geozones (owner_id, name, center, radius_m)
                SELECT :owner, 'z' || g, ST_SetSRID(ST_MakePoint(4.8 + g * 0.0001, 52.3), 4326), 100
                FROM generate_series(1, 3000) AS g
                """
            ),
            {"owner": owner},
        )
        await conn.execute(
            text(
                """
                INSERT INTO devices (device_id, position, recorded_at, received_at)
                SELECT 'dev-' || g, ST_SetSRID(ST_MakePoint(4.9, 52.37), 4326), now(), now()
                FROM generate_series(1, 5000) AS g
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO zone_presence (device_id, zone_id, entered_at, last_seen_at)
                SELECT 'dev-' || (row_number() OVER ()), id, now(), now()
                FROM geozones
                """
            )
        )
        zone_ids: list[uuid.UUID] = list(
            (await conn.execute(text("SELECT id FROM geozones LIMIT 40"))).scalars()
        )
        for table in ("geozones", "devices", "zone_presence"):
            await conn.execute(text(f"ANALYZE {table}"))
        devices = [f"dev-{i}" for i in range(1, 41)]
        plans = {
            "devices_pkey": await explain(conn, sql.LAST_RECORDED, {"device_ids": devices}),
            "zone_presence_pkey": await explain(conn, sql.PRESENCE, {"device_ids": devices}),
            "geozones_pkey": await explain(conn, sql.ZONE_RULES, {"zone_ids": zone_ids}),
        }
    for index, plan in plans.items():
        nodes = list(plan_nodes(plan))
        assert not [n for n in nodes if n["Node Type"] == "Seq Scan"], plan
        assert {n["Index Name"] for n in nodes if "Index Name" in n} == {index}, plan


async def explain(
    conn: AsyncConnection, statement: TextClause, params: dict[str, Any]
) -> dict[str, Any]:
    query = text("EXPLAIN (FORMAT JSON) " + statement.text)
    plan: list[dict[str, dict[str, Any]]] = (await conn.execute(query, params)).scalar_one()
    return plan[0]["Plan"]


def plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


async def test_an_alert_whose_publish_failed_is_swept_to_the_owner_exactly_once(
    db: AsyncEngine, js: JetStreamContext, provisioned: topology.Topology, nc: NatsClient
) -> None:
    owner = await make_user(db)
    await make_zone(db, owner)
    unlucky = BatchProcessor(
        db, UnreachableRelay(db, js), LiveRecorder(), owner="engine-test", stats=EngineStats()
    )
    await unlucky.apply(PARTITION, TOKEN, [report(T0)])
    assert await outbox_rows(db) == 1  # committed with the alert, waiting for the sweeper
    relay = OutboxRelay(db, js, sweep_min_age_s=0.0)
    received: list[Msg] = []

    async def collect(msg: Msg) -> None:
        received.append(msg)

    await nc.subscribe(subjects.live_events(owner), cb=collect)
    assert await relay.sweep_once() == 1
    await eventually(lambda: received)
    assert await outbox_rows(db) == 0
    assert await relay.sweep_once() == 0
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert json.loads(received[0].data)["data"]["kind"] == "enter"


async def test_every_applied_report_is_kept_in_the_track_and_late_ones_are_not(
    processor: BatchProcessor, db: AsyncEngine
) -> None:
    await processor.apply(PARTITION, TOKEN, [report(T0), report(T0 + 1_000), report(T0 + 2_000)])
    await processor.apply(PARTITION, TOKEN, [report(T0 + 500), report(T0 + 2_000)])  # late, replay
    async with db.connect() as conn:
        stored: list[int] = list(
            (
                await conn.execute(
                    text(
                        "SELECT (extract(epoch FROM recorded_at) * 1000)::bigint "
                        "FROM device_tracks WHERE device_id = 'veh-1' ORDER BY recorded_at"
                    )
                )
            ).scalars()
        )
    assert stored == [T0, T0 + 1_000, T0 + 2_000]
