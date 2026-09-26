"""The spatial queries: every distance decision is made by PostGIS, on the WGS84 spheroid.

* :func:`match_zones` — which active zones contain each report of a batch. One statement per
  batch; each report probes the GiST index on the zones' envelopes, and only candidates whose box
  contains the point pay for the exact ``ST_DWithin`` on ``geography``.
* :func:`devices_in_bbox` — latest positions inside a viewport (GiST on ``devices.position``).
* :func:`devices_near` — devices within a radius of a point (constant radius, so ``ST_DWithin``
  itself is indexable from the devices' side).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, INTEGER
from sqlalchemy.ext.asyncio import AsyncConnection

MATCH_ZONES_SQL = """
SELECT r.ord, z.id AS zone_id
FROM unnest(CAST(:ords AS integer[]), CAST(:lons AS float8[]), CAST(:lats AS float8[]))
     AS r(ord, lon, lat)
JOIN geozones AS z
  ON z.is_active
 AND z.envelope && ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)
 AND ST_DWithin(z.center, ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326)::geography, z.radius_m)
"""

_MATCH_ZONES = text(MATCH_ZONES_SQL).bindparams(
    bindparam("ords", type_=ARRAY(INTEGER)),
    bindparam("lons", type_=ARRAY(DOUBLE_PRECISION)),
    bindparam("lats", type_=ARRAY(DOUBLE_PRECISION)),
)

_DEVICES_IN_BBOX = text(
    """
    SELECT device_id,
           ST_Y(position::geometry) AS lat,
           ST_X(position::geometry) AS lon,
           recorded_at, speed_mps, heading_deg
    FROM devices
    WHERE position && ST_MakeEnvelope(:west, :south, :east, :north, 4326)::geography
      AND recorded_at > now() - make_interval(secs => :stale_s)
    ORDER BY device_id
    LIMIT :limit
    """
)

_DEVICES_NEAR = text(
    """
    SELECT device_id,
           ST_Y(position::geometry) AS lat,
           ST_X(position::geometry) AS lon,
           recorded_at, speed_mps, heading_deg
    FROM devices
    WHERE ST_DWithin(position, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius_m)
    ORDER BY device_id
    LIMIT :limit
    """
)


@dataclass(frozen=True, slots=True)
class DevicePosition:
    device_id: str
    lat: float
    lon: float
    recorded_at: datetime
    speed_mps: float | None
    heading_deg: float | None


async def match_zones(
    conn: AsyncConnection, lons: Sequence[float], lats: Sequence[float]
) -> list[list[uuid.UUID]]:
    """For report ``i`` (``lons[i]``, ``lats[i]``), the ids of the active zones containing it."""
    hits: list[list[uuid.UUID]] = [[] for _ in lons]
    if not hits:
        return hits
    result = await conn.execute(
        _MATCH_ZONES,
        {"ords": list(range(len(hits))), "lons": list(lons), "lats": list(lats)},
    )
    for ord_, zone_id in result:
        hits[ord_].append(zone_id)
    return hits


async def devices_in_bbox(
    conn: AsyncConnection,
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    stale_s: float,
    limit: int = 50_000,
) -> list[DevicePosition]:
    result = await conn.execute(
        _DEVICES_IN_BBOX,
        {
            "west": west,
            "south": south,
            "east": east,
            "north": north,
            "stale_s": stale_s,
            "limit": limit,
        },
    )
    return [DevicePosition(*row) for row in result]


async def devices_near(
    conn: AsyncConnection, *, lon: float, lat: float, radius_m: float, limit: int = 10_000
) -> list[DevicePosition]:
    result = await conn.execute(
        _DEVICES_NEAR, {"lon": lon, "lat": lat, "radius_m": radius_m, "limit": limit}
    )
    return [DevicePosition(*row) for row in result]
