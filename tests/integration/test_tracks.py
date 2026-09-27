"""Device tracks: rolling 10-minute partitions and the trail query."""

from __future__ import annotations

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


async def test_maintenance_creates_the_window_once(db: AsyncEngine) -> None:
    async with db.begin() as conn:
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
        await tracks.maintain(conn, retention_min=180)
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
        await tracks.maintain(conn, retention_min=30)
        assert await count(conn, "device_tracks_default") == 0


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
        await tracks.maintain(conn, retention_min=180)
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
