"""The spatial core against a real PostGIS: envelope, matching, index use, reverse queries."""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from geographiclib.geodesic import Geodesic
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from perimeter.domain.envelope import envelope
from perimeter.storage.spatial import (
    MATCH_ZONES_SQL,
    devices_in_bbox,
    devices_near,
    match_zones,
)

WGS84 = Geodesic.WGS84
AMSTERDAM = (4.9041, 52.3676)


async def make_user(conn: AsyncConnection, name: str = "alice") -> uuid.UUID:
    return (
        await conn.execute(
            text("INSERT INTO users (username) VALUES (:u) RETURNING id"), {"u": name}
        )
    ).scalar_one()


async def make_zone(
    conn: AsyncConnection,
    owner: uuid.UUID,
    lon: float,
    lat: float,
    radius_m: float,
    *,
    active: bool = True,
) -> uuid.UUID:
    return (
        await conn.execute(
            text(
                """
                INSERT INTO geozones (owner_id, name, center, radius_m, is_active)
                VALUES (:owner, 'z', ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                        :radius, :active)
                RETURNING id
                """
            ),
            {"owner": owner, "lon": lon, "lat": lat, "radius": radius_m, "active": active},
        )
    ).scalar_one()


async def naive_matches(
    conn: AsyncConnection, lons: list[float], lats: list[float]
) -> list[set[uuid.UUID]]:
    rows = await conn.execute(
        text(
            """
            SELECT r.ord, z.id
            FROM unnest(CAST(:lons AS float8[]), CAST(:lats AS float8[]))
                 WITH ORDINALITY AS r(lon, lat, ord)
            JOIN geozones z ON z.is_active AND ST_DWithin(
                z.center, ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)::geography, z.radius_m)
            """
        ),
        {"lons": lons, "lats": lats},
    )
    result: list[set[uuid.UUID]] = [set() for _ in lons]
    for ord_, zone_id in rows:
        result[ord_ - 1].add(zone_id)
    return result


async def test_sql_envelope_matches_the_python_mirror(db: AsyncEngine) -> None:
    rng = random.Random(5)
    cases = [(179.99, 10.0, 50_000.0), (-179.99, -30.0, 50_000.0), (0.0, 89.99, 5_000.0)]
    cases += [
        (rng.uniform(-180, 180), rng.uniform(-85, 85), rng.uniform(10, 100_000)) for _ in range(200)
    ]
    async with db.connect() as conn:
        for lon, lat, radius in cases:
            geojson: str = (
                await conn.execute(
                    text(
                        "SELECT ST_AsGeoJSON(perimeter_envelope("
                        "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r), 15)"
                    ),
                    {"lon": lon, "lat": lat, "r": radius},
                )
            ).scalar_one()
            shape = json.loads(geojson)
            polygons = (
                shape["coordinates"] if shape["type"] == "MultiPolygon" else [shape["coordinates"]]
            )
            sql_boxes = sorted(
                (
                    min(x for x, _ in ring[0]),
                    min(y for _, y in ring[0]),
                    max(x for x, _ in ring[0]),
                    max(y for _, y in ring[0]),
                )
                for ring in polygons
            )
            py_boxes = sorted(tuple(box) for box in envelope(lon, lat, radius))
            assert len(sql_boxes) == len(py_boxes)
            for got, want in zip(sql_boxes, py_boxes, strict=True):
                assert all(abs(a - b) < 1e-9 for a, b in zip(got, want, strict=True))


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    lon=st.floats(-180, 180),
    lat=st.floats(-84, 84),
    radius=st.floats(10, 100_000),
    seed=st.integers(0, 2**32 - 1),
)
async def test_boundary_points_are_decided_exactly_on_the_spheroid(
    db: AsyncEngine, lon: float, lat: float, radius: float, seed: int
) -> None:
    rng = random.Random(seed)
    tolerance = max(1e-6, radius * 1e-9)
    inside, outside = [], []
    for _ in range(24):
        azimuth = rng.uniform(-180, 180)
        near = WGS84.Direct(lat, lon, azimuth, radius - tolerance)
        far = WGS84.Direct(lat, lon, azimuth, radius + tolerance)
        inside.append((near["lon2"], near["lat2"]))
        outside.append((far["lon2"], far["lat2"]))
    async with db.begin() as conn:
        owner = await make_user(conn, f"u{seed % 100_000}")
        zone = await make_zone(conn, owner, lon, lat, radius)
        points = inside + outside
        hits = await match_zones(conn, [p[0] for p in points], [p[1] for p in points])
        await conn.rollback()
    assert all(h == [zone] for h in hits[: len(inside)]), "a point inside the circle was missed"
    assert all(h == [] for h in hits[len(inside) :]), "a point outside the circle matched"


async def test_indexed_matching_equals_the_naive_join(db: AsyncEngine) -> None:
    rng = random.Random(11)
    async with db.begin() as conn:
        owner = await make_user(conn)
        for i in range(400):
            az, dist = rng.uniform(0, 360), rng.uniform(0, 15_000)
            p = WGS84.Direct(AMSTERDAM[1], AMSTERDAM[0], az, dist)
            await make_zone(
                conn, owner, p["lon2"], p["lat2"], rng.uniform(50, 2_500), active=i % 7 != 0
            )
        lons, lats = [], []
        for _ in range(1_000):
            az, dist = rng.uniform(0, 360), rng.uniform(0, 17_000)
            p = WGS84.Direct(AMSTERDAM[1], AMSTERDAM[0], az, dist)
            lons.append(p["lon2"])
            lats.append(p["lat2"])
        await conn.execute(text("ANALYZE geozones"))
        indexed = [set(h) for h in await match_zones(conn, lons, lats)]
        naive = await naive_matches(conn, lons, lats)
    assert indexed == naive
    assert sum(len(h) for h in indexed) > 100  # the workload actually exercises matches


async def test_matching_probes_the_envelope_index(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        owner = await make_user(conn)
        for i in range(2_000):
            await make_zone(conn, owner, 4.8 + (i % 50) * 0.004, 52.3 + (i // 50) * 0.003, 300)
        await conn.execute(text("ANALYZE geozones"))
        plan: list[dict[str, dict[str, object]]] = (
            await conn.execute(
                text("EXPLAIN (FORMAT JSON) " + MATCH_ZONES_SQL),
                {"ords": list(range(500)), "lons": [4.9] * 500, "lats": [52.35] * 500},
            )
        ).scalar_one()
    nodes = list(_plan_nodes(plan[0]["Plan"]))
    on_zones = [n for n in nodes if n.get("Relation Name") == "geozones"]
    assert on_zones, "the plan does not touch geozones at all"
    assert all(n["Node Type"] == "Index Scan" for n in on_zones), on_zones
    assert {n.get("Index Name") for n in on_zones} == {"geozones_active_envelope_gix"}


def _plan_nodes(node: dict[str, object]) -> Iterator[dict[str, object]]:
    yield node
    for child in node.get("Plans", []):  # type: ignore[attr-defined]
        yield from _plan_nodes(child)


async def test_inactive_zones_never_match(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        owner = await make_user(conn)
        active = await make_zone(conn, owner, *AMSTERDAM, 500)
        await make_zone(conn, owner, *AMSTERDAM, 500, active=False)
        hits = await match_zones(conn, [AMSTERDAM[0]], [AMSTERDAM[1]])
    assert hits == [[active]]


async def test_empty_batch_matches_nothing(db: AsyncEngine) -> None:
    async with db.connect() as conn:
        assert await match_zones(conn, [], []) == []


async def test_viewport_and_radius_queries_use_latest_positions(db: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with db.begin() as conn:
        for device, (lon, lat), age in [
            ("near", (4.905, 52.368), 5),
            ("far", (5.2, 52.5), 5),
            ("stale", (4.904, 52.367), 3_600),
        ]:
            await conn.execute(
                text(
                    """
                    INSERT INTO devices (device_id, position, recorded_at, received_at)
                    VALUES (:d, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :t, :t)
                    """
                ),
                {"d": device, "lon": lon, "lat": lat, "t": now - timedelta(seconds=age)},
            )
        in_view = await devices_in_bbox(
            conn, west=4.85, south=52.33, east=4.95, north=52.40, stale_s=600
        )
        nearby = await devices_near(conn, lon=AMSTERDAM[0], lat=AMSTERDAM[1], radius_m=500)
    assert [d.device_id for d in in_view] == ["near"]
    assert {d.device_id for d in nearby} == {"near", "stale"}
    assert abs(in_view[0].lat - 52.368) < 1e-9
