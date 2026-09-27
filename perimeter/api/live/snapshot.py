"""Viewport snapshots: the latest known positions inside one tile, so a client starts from state.

When a session's viewport newly covers a tile prefix, the session gets one ``SNAPSHOT`` frame for
it, in the same binary format as live frames, built from the ``devices`` table. Frames are cached
per prefix for ``LIVE_SNAPSHOT_CACHE_S`` and requests are single-flight: however many sessions pan
onto the same area at once, one query runs. A semaphore bounds how many snapshot queries may hold
database connections, so the pool always keeps room for the REST API.

The index prefilter of :func:`~perimeter.storage.spatial.devices_in_bbox` compares 3-D boxes of
*great-circle* polygons. A lon/lat rectangle's edges along parallels become arcs that bulge
towards the pole, and a box wider than 180 degrees is not a polygon at all, so the plain envelope
can miss rows: points near the middle of the equatorward edge of a box straddling lon 0, 90, 180
or -90 (measured: ~1 km for a box of zoom-8 size, ~100 km at zoom-5 size), and most of the world
for the zoom-0 box. Queries here use boxes padded by the largest possible bulge, in slices at most
90 degrees wide, and the rows are filtered back to the exact tile with the tile arithmetic the
engine publishes with.
"""

from __future__ import annotations

import asyncio
import math
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

MAX_SLICE_DEG = 90.0
PAD_EPSILON_DEG = 1e-7
QUERY_TIMEOUT_S = 5.0

type DeviceQuery = Callable[[tiles.BBox], Awaitable[list[DevicePosition]]]


def max_bulge_deg(width_deg: float) -> float:
    """How far a great circle through two points of one parallel ``width_deg`` apart can rise
    above that parallel, over all latitudes (spherical): ``2 atan(sqrt(k)) - 90`` degrees with
    ``k = 1 / cos(width / 2)``."""
    k = 1.0 / math.cos(math.radians(width_deg / 2.0))
    return 2.0 * math.degrees(math.atan(math.sqrt(k))) - 90.0


def query_boxes(bounds: tiles.BBox) -> list[tiles.BBox]:
    """Boxes whose geography envelopes together contain every point of ``bounds``."""
    span = bounds.east - bounds.west
    slices = max(1, math.ceil(span / MAX_SLICE_DEG - 1e-9))
    width = span / slices
    pad = max_bulge_deg(width) + PAD_EPSILON_DEG
    south, north = bounds.south, bounds.north
    if south >= 0.0:  # northern hemisphere: the southern edge is the equatorward one
        south = max(south - pad, -90.0)
    elif north <= 0.0:
        north = min(north + pad, 90.0)
    edges = [bounds.west + i * width for i in range(slices)] + [bounds.east]
    return [tiles.BBox(edges[i], south, edges[i + 1], north) for i in range(slices)]


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
        found: dict[str, DevicePosition] = {}
        async with self._db.connect() as conn:
            for box in query_boxes(bounds):
                rows = await devices_in_bbox(
                    conn,
                    west=box.west,
                    south=box.south,
                    east=box.east,
                    north=box.north,
                    stale_s=self._stale_s,
                )
                found.update((row.device_id, row) for row in rows)
        return list(found.values())
