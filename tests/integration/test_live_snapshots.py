"""Snapshot frames against real PostGIS: exact tiles, the whole world, tiles of every size."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.live.snapshot import SnapshotService
from perimeter.domain import tiles
from perimeter.storage.spatial import devices_in_bbox
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


async def put_grid(db: AsyncEngine, box: tiles.BBox, columns: int, rows: int) -> set[str]:
    """Devices on a grid over ``box`` (edges included); returns their ids."""
    ids = []
    async with db.begin() as conn:
        for i in range(columns + 1):
            for j in range(rows + 1):
                lon = box.west + (box.east - box.west) * i / columns
                lat = box.south + (box.north - box.south) * j / rows
                device_id = f"g-{i}-{j}"
                ids.append(device_id)
                await conn.execute(
                    text(
                        """
                        INSERT INTO devices (device_id, position, recorded_at, received_at)
                        VALUES (:d, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                                now(), now())
                        """
                    ),
                    {"d": device_id, "lon": lon, "lat": lat},
                )
    return set(ids)


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
async def test_viewport_queries_find_every_device_of_wide_and_edge_straddling_boxes(
    db: AsyncEngine, box: tiles.BBox
) -> None:
    # Boxes whose parallels a great-circle test would bow away from: the planar test must not.
    expected = await put_grid(db, box, columns=40, rows=10)
    async with db.connect() as conn:
        found = await devices_in_bbox(
            conn, west=box.west, south=box.south, east=box.east, north=box.north, stale_s=600
        )
    assert {row.device_id for row in found} == expected


async def test_tiles_of_every_zoom_return_exactly_their_devices(db: AsyncEngine) -> None:
    await put_grid(db, tiles.BBox(3.0, 51.0, 7.0, 54.0), columns=40, rows=30)
    service = snapshots(db)
    for zoom in range(13):
        tile = tiles.tile_for(*AMSTERDAM, zoom)
        prefix = tiles.quadkey(tile)
        async with db.connect() as conn:
            everything = await devices_in_bbox(
                conn, west=-180, south=-85.06, east=180, north=85.06, stale_s=600
            )
        expected = {d.device_id for d in everything if tiles.tile_for(d.lon, d.lat, zoom) == tile}
        assert await ids_in(service, prefix) == expected, f"zoom {zoom}"


async def test_antimeridian_viewports_are_searched_on_both_sides(db: AsyncEngine) -> None:
    await put_device(db, "east-of-line", 179.9, 10.0)
    await put_device(db, "west-of-line", -179.9, 10.0)
    await put_device(db, "far-away", 0.0, 10.0)
    async with db.connect() as conn:
        found = await devices_in_bbox(
            conn, west=179.0, south=5.0, east=-179.0, north=15.0, stale_s=600
        )
    assert [row.device_id for row in found] == ["east-of-line", "west-of-line"]


async def test_viewport_queries_use_the_planar_index(db: AsyncEngine) -> None:
    await put_grid(db, tiles.BBox(4.0, 52.0, 5.0, 53.0), columns=60, rows=60)
    async with db.begin() as conn:
        await conn.execute(text("ANALYZE devices"))
        plan: str = (
            (
                await conn.execute(
                    text(
                        "EXPLAIN SELECT device_id FROM devices "
                        "WHERE position::geometry && ST_MakeEnvelope(4.5, 52.5, 4.51, 52.51, 4326)"
                    )
                )
            )
            .scalars()
            .all()
            .__str__()
        )
    assert "devices_lonlat_gix" in plan
