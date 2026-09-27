"""Device tracks: every applied report, kept for ``TRACK_RETENTION_MIN`` minutes.

The engine inserts the reports of each batch in the batch's own transaction (one set-based
statement, :data:`perimeter.engine.sql.INSERT_TRACKS`). The table is range-partitioned by event
time in 10-minute slots, and :func:`maintain` keeps the window rolling through the migration's
``perimeter_maintain_tracks`` function: expired slots are dropped whole, which costs the same
however many reports they hold, and new slots are attached ahead of time. Reports no slot covers
land in a default partition; the next round purges the expired ones and moves the rest into the
slot it creates for them, so maintenance that could not run for a while catches up by itself. A
device's trail is then one index range scan on ``(device_id, recorded_at)`` in the few newest
partitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.storage.zones import real

_MAINTAIN = text(
    "SELECT created, dropped, moved, purged, failed"
    " FROM perimeter_maintain_tracks(make_interval(mins => :retention_min))"
)

_RECENT = text(
    """
    SELECT (extract(epoch FROM recorded_at) * 1000)::bigint AS recorded_ms,
           ST_Y(position::geometry) AS lat,
           ST_X(position::geometry) AS lon,
           speed_mps, heading_deg
    FROM (
        SELECT recorded_at, position, speed_mps, heading_deg
        FROM device_tracks
        WHERE device_id = :device_id AND recorded_at > :since
        ORDER BY recorded_at DESC
        LIMIT :limit
    ) AS newest
    ORDER BY recorded_at
    """
)


@dataclass(frozen=True, slots=True)
class TrackPoint:
    recorded_at_ms: int
    lat: float
    lon: float
    speed_mps: float | None
    heading_deg: float | None


@dataclass(frozen=True, slots=True)
class Maintenance:
    created: int  # slots attached
    dropped: int  # expired slots dropped
    moved: int  # reports moved out of the default partition into their new slot
    purged: int  # expired reports removed from the default partition
    failed: int  # steps that could not get their lock in time; retried next round


async def maintain(conn: AsyncConnection, *, retention_min: int) -> Maintenance:
    """Create the upcoming slots and drop the expired ones (a no-op most of the time)."""
    row = (await conn.execute(_MAINTAIN, {"retention_min": retention_min})).one()
    return Maintenance(
        created=int(row.created),
        dropped=int(row.dropped),
        moved=int(row.moved),
        purged=int(row.purged),
        failed=int(row.failed),
    )


async def recent(
    conn: AsyncConnection, device_id: str, *, since: datetime, limit: int
) -> tuple[list[TrackPoint], bool]:
    """The device's reports after ``since``, oldest first: at most ``limit``, the newest ones.

    The flag tells whether that is everything in the window (``False`` when ``limit`` cut it).
    """
    rows = (
        await conn.execute(_RECENT, {"device_id": device_id, "since": since, "limit": limit + 1})
    ).all()
    complete = len(rows) <= limit
    kept = rows if complete else rows[1:]
    return [
        TrackPoint(int(r.recorded_ms), r.lat, r.lon, real(r.speed_mps), real(r.heading_deg))
        for r in kept
    ], complete
