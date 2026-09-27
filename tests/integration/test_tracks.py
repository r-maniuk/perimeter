"""Device tracks: rolling 10-minute partitions and the trail query."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from perimeter.storage import tracks


async def slots(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        text(
            """
            SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = 'device_tracks'::regclass ORDER BY c.relname
            """
        )
    )
    return [name for (name,) in rows]


async def put(conn: AsyncConnection, device: str, at: datetime, lon: float = 4.9) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO device_tracks (device_id, recorded_at, position, speed_mps, heading_deg)
            VALUES (:d, :at, ST_SetSRID(ST_MakePoint(:lon, 52.37), 4326)::geography, 8.1, 90)
            """
        ),
        {"d": device, "at": at, "lon": lon},
    )


async def count(conn: AsyncConnection, table: str = "device_tracks") -> int:
    total: int = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return total


async def settle(conn: AsyncConnection, *, retention_min: int) -> tracks.Maintenance:
    """Step until the window is complete (a long one takes more than one step)."""
    total = tracks.Maintenance(created=0, dropped=0, moved=0, purged=0, failed=0)
    for _ in range(50):
        total += await tracks.maintain(conn, retention_min=retention_min)
        if not total.more:
            return total
    raise AssertionError("maintenance never settled")


async def test_maintenance_creates_the_window_once(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        for name in await slots(conn):  # whatever earlier tests left: start from no window
            if name != "device_tracks_default":
                await conn.execute(text(f"DROP TABLE {name}"))
        first = await tracks.maintain(conn, retention_min=30)
        second = await tracks.maintain(conn, retention_min=30)
        names = await slots(conn)
    # 30 minutes back and 20 ahead, in 10-minute slots: 6 or 7 depending on the clock.
    assert 6 <= first.created <= 7
    assert second.created == 0
    assert "device_tracks_default" in names
    assert len(names) == first.created + 1


async def test_expired_slots_are_dropped_whole_and_recent_ones_kept(db: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with db.begin() as conn:
        await settle(conn, retention_min=180)
        await put(conn, "veh-1", now - timedelta(minutes=150))
        await put(conn, "veh-1", now - timedelta(minutes=5))
        before = len(await slots(conn))
        rolled = await tracks.maintain(conn, retention_min=30)
        after = len(await slots(conn))
        remaining = await count(conn)
    assert rolled.dropped >= 14  # 180 minutes shrunk to 30: fifteen slots or so went away
    assert after == before - rolled.dropped
    assert remaining == 1


async def test_reports_older_than_every_slot_are_cleared(db: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with db.begin() as conn:
        await tracks.maintain(conn, retention_min=30)
        await put(conn, "late", now - timedelta(hours=5))  # no slot covers it: the default one
        assert await count(conn, "device_tracks_default") == 1
        rolled = await tracks.maintain(conn, retention_min=30)
        assert await count(conn, "device_tracks_default") == 0
    assert rolled.purged == 1


def slot_start(at: datetime) -> datetime:
    return at.replace(minute=at.minute - at.minute % 10, second=0, microsecond=0)


def slot_of(at: datetime) -> str:
    return f"device_tracks_{slot_start(at):%Y%m%d%H%M}"


async def test_reports_that_arrived_while_maintenance_lagged_move_into_their_slot(
    db: AsyncEngine,
) -> None:
    # Once the default partition holds rows of a slot's range, that slot can no longer simply be
    # created; the maintenance must move them in, or it would fail on every round from then on.
    now = datetime.now(UTC)
    async with db.begin() as conn:
        await tracks.maintain(conn, retention_min=30)
        await conn.execute(text(f"DROP TABLE {slot_of(now)}"))  # as if it was never created
        await put(conn, "veh-7", now)
        assert await count(conn, "device_tracks_default") == 1
        rolled = await tracks.maintain(conn, retention_min=30)
        assert await count(conn, "device_tracks_default") == 0
        assert await count(conn, slot_of(now)) == 1
        assert await count(conn) == 1
    assert (rolled.created, rolled.moved, rolled.failed) == (1, 1, 0)


async def test_a_step_that_cannot_get_its_lock_waits_for_the_next_round(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await settle(conn, retention_min=60)
    async with db.connect() as backup, backup.begin():
        # What pg_dump holds on every table it reads, for as long as it runs.
        await backup.execute(text("LOCK TABLE device_tracks IN ACCESS SHARE MODE"))
        started = time.monotonic()
        async with db.begin() as conn:
            blocked = await tracks.maintain(conn, retention_min=30)
        waited = time.monotonic() - started
    # The first expired slot gave up after half a second, and the rest of its phase with it: the
    # same holder has them all.
    assert (blocked.dropped, blocked.failed, blocked.more) == (0, 1, False)
    assert waited < 2
    async with db.begin() as conn:
        caught_up = await tracks.maintain(conn, retention_min=30)
    assert caught_up.failed == 0
    assert caught_up.dropped >= 3  # 60 minutes shrunk to 30


async def test_recent_returns_the_newest_points_oldest_first(db: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with db.begin() as conn:
        await tracks.maintain(conn, retention_min=30)
        for index in range(6):
            await put(conn, "veh-9", now - timedelta(seconds=60 - index * 10), lon=4.0 + index / 10)
        await put(conn, "veh-8", now - timedelta(seconds=5))
        since = now - timedelta(minutes=5)
        everything, complete = await tracks.recent(conn, "veh-9", since=since, limit=10)
        capped, capped_complete = await tracks.recent(conn, "veh-9", since=since, limit=4)
    assert [round(p.lon, 6) for p in everything] == [4.0, 4.1, 4.2, 4.3, 4.4, 4.5]
    assert complete
    assert [round(p.lon, 6) for p in capped] == [4.2, 4.3, 4.4, 4.5]  # the newest four
    assert not capped_complete
    assert everything[0].speed_mps == 8.1  # float4 without its binary noise
    assert everything[0].heading_deg == 90.0


async def test_trail_reads_touch_only_recent_slots(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await settle(conn, retention_min=180)
        plan = "\n".join(
            row[0]
            for row in await conn.execute(
                text(
                    "EXPLAIN SELECT * FROM device_tracks WHERE device_id = 'veh-1' "
                    "AND recorded_at > now() - interval '10 minutes'"
                )
            )
        )
    assert "Subplans Removed" in plan or plan.count("device_tracks_2") <= 4


async def test_a_backlog_larger_than_a_step_moves_over_in_bounded_steps(db: AsyncEngine) -> None:
    # Two slots missed while maintenance could not run: all their reports wait in the default
    # partition, more than one step may move.
    current = slot_start(datetime.now(UTC))
    previous = current - timedelta(minutes=10)
    async with db.begin() as conn:
        await settle(conn, retention_min=30)
        for start in (previous, current):
            await conn.execute(text(f"DROP TABLE {slot_of(start)}"))
            for second in range(60):
                await put(conn, f"veh-{second}", start + timedelta(seconds=second))
        steps = [await tracks.maintain(conn, retention_min=30, budget=25)]
        # Moved into the slot's table, not attached yet: out of sight until it is.
        assert await count(conn) == 95
        while steps[-1].more:
            assert len(steps) < 20
            steps.append(await tracks.maintain(conn, retention_min=30, budget=25))
        assert await count(conn, "device_tracks_default") == 0
        assert await count(conn, slot_of(previous)) == 60
        assert await count(conn, slot_of(current)) == 60
        assert await count(conn) == 120
    assert [step.moved for step in steps] == [25, 25, 10, 25, 25, 10]
    assert sum(step.created for step in steps) == 2
    assert all(step.failed == 0 for step in steps)


async def test_roll_keeps_each_step_committed_and_finishes_the_backlog(db: AsyncEngine) -> None:
    current = slot_start(datetime.now(UTC))
    async with db.begin() as conn:
        await settle(conn, retention_min=30)
        await conn.execute(text(f"DROP TABLE {slot_of(current)}"))
        for second in range(50):
            await put(conn, "veh-3", current + timedelta(seconds=second))
    # Out of time after the first step: what it did is committed, and it says work is left.
    partial = await tracks.roll(db, retention_min=30, time_budget_s=0, budget=20)
    assert (partial.moved, partial.more) == (20, True)
    async with db.connect() as conn:
        assert await count(conn, slot_of(current)) == 20
    done = await tracks.roll(db, retention_min=30, time_budget_s=30, budget=20)
    assert (done.created, done.moved, done.more) == (1, 30, False)
    async with db.connect() as conn:
        assert await count(conn, "device_tracks_default") == 0
        assert await count(conn) == 50


async def test_a_report_repeated_after_its_first_copy_moved_on_is_kept_once(
    db: AsyncEngine,
) -> None:
    current = slot_start(datetime.now(UTC))
    async with db.begin() as conn:
        await settle(conn, retention_min=30)
        await conn.execute(text(f"DROP TABLE {slot_of(current)}"))
        for second in range(30):
            await put(conn, "veh-4", current + timedelta(seconds=second))
        first = await tracks.maintain(conn, retention_min=30, budget=20)
        assert (first.moved, first.more) == (20, True)
        # A retry of a report whose first copy already moved: the default partition takes it.
        await put(conn, "veh-4", current)
        rest = await tracks.maintain(conn, retention_min=30, budget=20)
        assert await count(conn, "device_tracks_default") == 0
        assert await count(conn) == 30
    assert (rest.created, rest.moved, rest.failed, rest.more) == (1, 10, 0, False)


async def test_a_slot_that_expires_before_it_is_attached_is_dropped(db: AsyncEngine) -> None:
    older = slot_start(datetime.now(UTC)) - timedelta(minutes=20)
    async with db.begin() as conn:
        await settle(conn, retention_min=30)
        await conn.execute(text(f"DROP TABLE {slot_of(older)}"))
        for second in range(30):
            await put(conn, "veh-5", older + timedelta(seconds=second))
        started = await tracks.maintain(conn, retention_min=30, budget=20)
        assert (started.moved, started.more) == (20, True)
        # Retention shrinks past that slot before it could be attached.
        shrunk = await settle(conn, retention_min=10)
        exists: str | None = (
            await conn.execute(text("SELECT to_regclass(:name)"), {"name": slot_of(older)})
        ).scalar_one()
        assert exists is None
        assert await count(conn, "device_tracks_default") == 0
        await settle(conn, retention_min=30)
    assert shrunk.purged == 10  # the rest never left the default partition
    assert shrunk.failed == 0


async def test_a_long_window_is_attached_a_few_slots_per_step(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await settle(conn, retention_min=30)
        first = await tracks.maintain(conn, retention_min=240)
        rest = await settle(conn, retention_min=240)
        attached = len(await slots(conn)) - 1
        await settle(conn, retention_min=30)
    assert (first.created, first.more) == (12, True)
    assert not rest.more
    assert 25 <= attached <= 27  # four hours back and twenty minutes ahead
