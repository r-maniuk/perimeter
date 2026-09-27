"""Snapshot frames against real PostGIS: exact tiles, the whole world, conservative query boxes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.live.snapshot import SnapshotService, query_boxes
from perimeter.domain import tiles
from perimeter.wire.frames import FrameKind, decode_tile

AMSTERDAM = (4.9041, 52.3676)
WORLD = {
    "amsterdam": AMSTERDAM,
    "sydney": (151.2093, -33.8688),
    "buenos-aires": (-58.3816, -34.6037),
    "anchorage": (-149.9003, 61.2181),
    "tokyo": (139.6917, 35.6895),
    "date-line-north": (179.99, 0.5),
    "date-line-south": (-179.99, -0.5),
    "quito": (-78.4678, -0.1807),
}


async def put_device(
    db: AsyncEngine, device_id: str, lon: float, lat: float, age_s: int = 5
) -> None:
    at = datetime.now(UTC) - timedelta(seconds=age_s)
    async with db.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO devices (device_id, position, recorded_at, received_at)
                VALUES (:d, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :t, :t)
                """
            ),
            {"d": device_id, "lon": lon, "lat": lat, "t": at},
        )


def snapshots(db: AsyncEngine) -> SnapshotService:
    return SnapshotService(db, stale_s=600, cache_s=0, concurrency=2)


async def ids_in(service: SnapshotService, prefix: str) -> set[str]:
    frame = decode_tile(await service.frame(prefix))
    tile = tiles.tile_of_quadkey(prefix)
    assert frame.kind == FrameKind.SNAPSHOT
    assert (frame.zoom, frame.x, frame.y) == (tile.z, tile.x, tile.y)
    return {point.device_id for point in frame.points}


async def test_a_tile_snapshot_holds_exactly_the_fresh_devices_of_that_tile(
    db: AsyncEngine,
) -> None:
    prefix = tiles.quadkey_for(*AMSTERDAM, 12)
    bounds = tiles.tile_bounds(tiles.tile_of_quadkey(prefix))
    middle = (bounds.south + bounds.north) / 2
    await put_device(db, "centre", *AMSTERDAM)
    await put_device(db, "west-edge", bounds.west, middle)
    await put_device(db, "just-east", bounds.east + 1e-7, middle)
    await put_device(db, "just-south", AMSTERDAM[0], bounds.south - 1e-7)
    await put_device(db, "stale", *AMSTERDAM, age_s=3_600)
    await put_device(db, "rotterdam", 4.4777, 51.9244)
    assert await ids_in(snapshots(db), prefix) == {"centre", "west-edge"}


async def test_the_whole_world_and_its_quarters_find_devices_everywhere(db: AsyncEngine) -> None:
    for device_id, (lon, lat) in WORLD.items():
        await put_device(db, device_id, lon, lat)
    service = snapshots(db)
    assert await ids_in(service, "") == set(WORLD)
    for quarter in "0123":
        tile = tiles.tile_of_quadkey(quarter)
        expected = {d for d, (lon, lat) in WORLD.items() if tiles.tile_for(lon, lat, 1) == tile}
        assert await ids_in(service, quarter) == expected


_MISSES = text(
    """
    SELECT count(*)
    FROM (SELECT CAST(:w AS float8) AS w, CAST(:s AS float8) AS s,
                 CAST(:e AS float8) AS e, CAST(:n AS float8) AS n) AS box,
         generate_series(0, 200) AS gx, generate_series(0, 50) AS gy
    WHERE NOT EXISTS (
        SELECT 1
        FROM unnest(CAST(:ws AS float8[]), CAST(:ss AS float8[]),
                    CAST(:es AS float8[]), CAST(:ns AS float8[])) AS b(w, s, e, n)
        WHERE ST_SetSRID(ST_MakePoint(box.w + (box.e - box.w) * gx / 200.0,
                                      box.s + (box.n - box.s) * gy / 50.0), 4326)::geography
              && ST_MakeEnvelope(b.w, b.s, b.e, b.n, 4326)::geography
    )
    """
).bindparams(
    bindparam("ws", type_=ARRAY(DOUBLE_PRECISION)),
    bindparam("ss", type_=ARRAY(DOUBLE_PRECISION)),
    bindparam("es", type_=ARRAY(DOUBLE_PRECISION)),
    bindparam("ns", type_=ARRAY(DOUBLE_PRECISION)),
)


async def misses(db: AsyncEngine, box: tiles.BBox, cover: list[tiles.BBox]) -> int:
    """Points of a 201 x 51 grid over ``box`` that no geography envelope of ``cover`` finds."""
    async with db.connect() as conn:
        found: int = (
            await conn.execute(
                _MISSES,
                {
                    "w": box.west,
                    "s": box.south,
                    "e": box.east,
                    "n": box.north,
                    "ws": [b.west for b in cover],
                    "ss": [b.south for b in cover],
                    "es": [b.east for b in cover],
                    "ns": [b.north for b in cover],
                },
            )
        ).scalar_one()
    return found


@pytest.mark.parametrize(
    "box",
    [
        tiles.BBox(-0.703125, 51.6180165487737, 0.703125, 52.0524970588585),  # zoom-8 size, lon 0
        tiles.BBox(-5.625, 48.9224992637582, 5.625, 55.7765730186677),  # zoom-5 size, lon 0
        tiles.BBox(-5.625, -55.7765730186677, 5.625, -48.9224992637582),  # southern hemisphere
        tiles.BBox(84.375, 40.9798980696201, 95.625, 48.9224992637582),  # straddles lon 90
        tiles.BBox(-180.0, 0.0, 180.0, 85.0511287798066),  # wider than a hemisphere
    ],
)
async def test_padded_query_boxes_miss_nothing_where_the_plain_envelope_does(
    db: AsyncEngine, box: tiles.BBox
) -> None:
    assert await misses(db, box, [box]) > 0  # the probe is sharp enough to see the problem
    assert await misses(db, box, query_boxes(box)) == 0


async def test_padded_query_boxes_cover_real_tiles_at_every_zoom(db: AsyncEngine) -> None:
    for zoom in range(13):
        box = tiles.tile_bounds(tiles.tile_for(*AMSTERDAM, zoom))
        assert await misses(db, box, query_boxes(box)) == 0, f"zoom {zoom}"
