"""Viewport snapshots: the latest known positions inside one tile, so a client starts from state.

When a session's viewport newly covers a tile prefix, the session gets one ``SNAPSHOT`` frame for
it, in the same binary format as live frames, built from the ``devices`` table. Frames are cached
per prefix for ``LIVE_SNAPSHOT_CACHE_S`` and requests are single-flight: however many sessions pan
onto the same area at once, one query runs. A semaphore bounds how many snapshot queries may hold
database connections, so the pool always keeps room for the REST API.

Rows come from :func:`~perimeter.storage.spatial.devices_in_bbox` on the tile's exact lon/lat
bounds and are filtered with the same tile arithmetic the engine publishes with, so a device on a
shared tile edge appears in exactly one tile's snapshot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import partial

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.live import metrics
from perimeter.domain import tiles
from perimeter.domain.reports import datetime_to_ms
from perimeter.storage.spatial import DevicePosition, devices_in_bbox
from perimeter.wire.frames import FrameKind, FramePoint, encode_tile

log = structlog.get_logger(__name__)

QUERY_TIMEOUT_S = 5.0

type DeviceQuery = Callable[[tiles.BBox], Awaitable[list[DevicePosition]]]


class SnapshotService:
    def __init__(
        self,
        db: AsyncEngine | None,
        *,
        stale_s: float,
        cache_s: float,
        concurrency: int,
        query: DeviceQuery | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._stale_s = stale_s
        self._cache_s = cache_s
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self._query = query or self._devices_in
        self._clock = clock
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._inflight: dict[str, asyncio.Task[bytes]] = {}

    async def frame(self, prefix: str) -> bytes:
        """The snapshot frame of the tile ``prefix`` (cached, shared with concurrent callers)."""
        cached = self._cache.get(prefix)
        if cached is not None and cached[0] > self._clock():
            metrics.SNAPSHOTS.labels("cached").inc()
            return cached[1]
        task = self._inflight.get(prefix)
        if task is None:
            task = asyncio.create_task(self._build(prefix), name="live-snapshot")
            self._inflight[prefix] = task
            task.add_done_callback(partial(self._settle, prefix))
            metrics.SNAPSHOTS.labels("queried").inc()
        else:
            metrics.SNAPSHOTS.labels("shared").inc()
        # Shielded: a caller that goes away must not cancel the query others are waiting for.
        return await asyncio.shield(task)

    def prune(self) -> None:
        """Forget expired frames (called periodically; lookups ignore them anyway)."""
        now = self._clock()
        for prefix in [p for p, (expires, _) in self._cache.items() if expires <= now]:
            del self._cache[prefix]

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()

    def _settle(self, prefix: str, task: asyncio.Task[bytes]) -> None:
        if self._inflight.get(prefix) is task:
            del self._inflight[prefix]
        if task.cancelled():
            return
        error = (
            task.exception()
        )  # retrieved here, so a failure nobody awaited is not reported twice
        if error is not None:
            metrics.SNAPSHOTS.labels("failed").inc()
            log.warning("live.snapshot_failed", prefix=prefix, error=repr(error))
            return
        if self._cache_s > 0:
            self._cache[prefix] = (self._clock() + self._cache_s, task.result())

    async def _build(self, prefix: str) -> bytes:
        tile = tiles.tile_of_quadkey(prefix)
        started = time.perf_counter()
        async with self._slots, asyncio.timeout(QUERY_TIMEOUT_S):
            rows = await self._query(tiles.tile_bounds(tile))
        points = [
            FramePoint(
                device_id=row.device_id,
                lat=row.lat,
                lon=row.lon,
                recorded_at_ms=datetime_to_ms(row.recorded_at),
                speed_mps=row.speed_mps,
                heading_deg=row.heading_deg,
            )
            for row in rows
            if tiles.tile_for(row.lon, row.lat, tile.z) == tile
        ]
        frame = encode_tile(FrameKind.SNAPSHOT, tile.z, tile.x, tile.y, points)
        metrics.SNAPSHOT_SECONDS.observe(time.perf_counter() - started)
        return frame

    async def _devices_in(self, bounds: tiles.BBox) -> list[DevicePosition]:
        assert self._db is not None, "a database engine is required without a custom query"
        async with self._db.connect() as conn:
            return await devices_in_bbox(
                conn,
                west=bounds.west,
                south=bounds.south,
                east=bounds.east,
                north=bounds.north,
                stale_s=self._stale_s,
            )
