"""Device tracks: every applied report, kept for ``TRACK_RETENTION_MIN`` minutes.

The engine inserts the reports of each batch in the batch's own transaction (one set-based
statement, :data:`perimeter.engine.sql.INSERT_TRACKS`). The table is range-partitioned by event
time in 10-minute slots, and :func:`roll` keeps the window rolling through the migration's
``perimeter_maintain_tracks`` function: expired slots are dropped whole, which costs the same
however many reports they hold, and new slots are attached ahead of time. Reports no slot covers
land in a default partition; maintenance purges the expired ones and moves the rest into their
slot, in steps of bounded size that each commit on their own, so maintenance that could not run
for a while (a long backup holds the locks it needs) catches up by itself, however large the
backlog. A device's trail is then one index range scan on ``(device_id, recorded_at)`` in the
few newest partitions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from perimeter.storage.zones import real

_MAINTAIN = text(
    "SELECT created, dropped, moved, purged, failed, more"
    " FROM perimeter_maintain_tracks(make_interval(mins => :retention_min), budget => :budget)"
)

#: Reports one step moves or purges at most: well under a second, far inside any statement timeout.
STEP_ROWS = 20_000

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
    moved: int  # reports moved out of the default partition into their slot
    purged: int  # expired reports removed from the default partition
    failed: int  # steps that could not get their lock in time; retried next round
    more: bool = False  # work is left that the next step can do right away

    def __add__(self, later: Maintenance) -> Maintenance:
        return Maintenance(
            created=self.created + later.created,
            dropped=self.dropped + later.dropped,
            moved=self.moved + later.moved,
            purged=self.purged + later.purged,
            failed=self.failed + later.failed,
            more=later.more,
        )


async def maintain(
    conn: AsyncConnection, *, retention_min: int, budget: int = STEP_ROWS
) -> Maintenance:
    """One bounded step: drop, purge, move or attach as much as ``budget`` rows allow.

    A no-op most of the time. The step's work is kept only if the caller commits it; ``more``
    says another step has work to do right away.
    """
    row = (await conn.execute(_MAINTAIN, {"retention_min": retention_min, "budget": budget})).one()
    return Maintenance(
        created=int(row.created),
        dropped=int(row.dropped),
        moved=int(row.moved),
        purged=int(row.purged),
        failed=int(row.failed),
        more=bool(row.more),
    )


async def roll(
    db: AsyncEngine, *, retention_min: int, time_budget_s: float, budget: int = STEP_ROWS
) -> Maintenance:
    """Step until nothing is left or ``time_budget_s`` is spent, each step in its own transaction.

    What a step did stays done even if a later one fails; the total says whether work was left.
    """
    deadline = time.monotonic() + time_budget_s
    total = Maintenance(created=0, dropped=0, moved=0, purged=0, failed=0)
    while True:
        async with db.begin() as conn:
            step = await maintain(conn, retention_min=retention_min, budget=budget)
        total += step
        if not step.more or time.monotonic() >= deadline:
            return total


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
