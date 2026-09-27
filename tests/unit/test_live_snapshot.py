from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from perimeter.api.live.snapshot import SnapshotService
from perimeter.domain import tiles
from perimeter.domain.reports import datetime_to_ms
from perimeter.storage.spatial import DevicePosition
from perimeter.wire.frames import FrameKind, decode_tile

AMSTERDAM = (4.9041, 52.3676)
PREFIX = tiles.quadkey_for(*AMSTERDAM, 12)


def position(device: str, lon: float, lat: float) -> DevicePosition:
    return DevicePosition(
        device_id=device,
        lat=lat,
        lon=lon,
        recorded_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        speed_mps=None,
        heading_deg=90.0,
    )


class Query:
    def __init__(self, rows: list[DevicePosition], *, delay: float = 0.0) -> None:
        self.rows = rows
        self.delay = delay
        self.calls: list[tiles.BBox] = []
        self.running = 0
        self.peak = 0
        self.error: Exception | None = None

    async def __call__(self, bounds: tiles.BBox) -> list[DevicePosition]:
        self.calls.append(bounds)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.rows
        finally:
            self.running -= 1


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def service(query: Query, *, clock: Clock | None = None, concurrency: int = 4) -> SnapshotService:
    return SnapshotService(
        None,
        stale_s=600,
        cache_s=1.0,
        concurrency=concurrency,
        query=query,
        clock=clock or Clock(),
    )


async def test_a_snapshot_holds_exactly_the_devices_inside_the_tile() -> None:
    bounds = tiles.tile_bounds(tiles.tile_of_quadkey(PREFIX))
    inside = position("in", *AMSTERDAM)
    edge = position("edge", bounds.west, (bounds.south + bounds.north) / 2)
    outside = position("out", bounds.east + 0.001, AMSTERDAM[1])
    snapshots = service(Query([inside, edge, outside]))
    decoded = decode_tile(await snapshots.frame(PREFIX))
    tile = tiles.tile_of_quadkey(PREFIX)
    assert decoded.kind == FrameKind.SNAPSHOT
    assert (decoded.zoom, decoded.x, decoded.y) == (tile.z, tile.x, tile.y)
    assert [p.device_id for p in decoded.points] == ["in", "edge"]
    assert decoded.points[0].heading_deg == 90.0
    assert decoded.points[0].recorded_at_ms == datetime_to_ms(inside.recorded_at)


async def test_concurrent_requests_share_one_query_and_the_frame_is_cached() -> None:
    query = Query([position("a", *AMSTERDAM)], delay=0.05)
    clock = Clock()
    snapshots = service(query, clock=clock)
    first, second = await asyncio.gather(snapshots.frame(PREFIX), snapshots.frame(PREFIX))
    assert first == second
    assert len(query.calls) == 1
    clock.now += 0.5
    assert await snapshots.frame(PREFIX) == first
    assert len(query.calls) == 1
    clock.now += 0.6
    snapshots.prune()
    await snapshots.frame(PREFIX)
    assert len(query.calls) == 2


async def test_a_failed_query_reaches_every_waiter_and_is_not_cached() -> None:
    query = Query([], delay=0.02)
    query.error = TimeoutError("database busy")
    snapshots = service(query)
    results = await asyncio.gather(
        snapshots.frame(PREFIX), snapshots.frame(PREFIX), return_exceptions=True
    )
    assert all(isinstance(r, TimeoutError) for r in results)
    query.error = None
    assert decode_tile(await snapshots.frame(PREFIX)).points == ()
    assert len(query.calls) == 2


async def test_the_database_sees_a_bounded_number_of_snapshot_queries() -> None:
    query = Query([], delay=0.02)
    snapshots = service(query, concurrency=2)
    prefixes = [PREFIX[:-1] + digit for digit in "0123"]
    await asyncio.gather(*(snapshots.frame(p) for p in prefixes))
    assert len(query.calls) == 4
    assert query.peak == 2


async def test_a_caller_that_goes_away_does_not_cancel_the_query_for_others() -> None:
    query = Query([position("a", *AMSTERDAM)], delay=0.05)
    snapshots = service(query)
    leaving = asyncio.create_task(snapshots.frame(PREFIX))
    staying = asyncio.create_task(snapshots.frame(PREFIX))
    await asyncio.sleep(0.01)
    leaving.cancel()
    frame = await staying
    assert [p.device_id for p in decode_tile(frame).points] == ["a"]
    assert len(query.calls) == 1
    await snapshots.close()
