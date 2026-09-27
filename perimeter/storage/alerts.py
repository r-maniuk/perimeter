"""Alert history of one owner, newest first, with optional filters.

The engine writes alerts; this module only reads them. Pages are keyset ranges over the index
``alerts (owner_id, occurred_at DESC, id DESC)``: the first page and the thousandth cost the same,
and an alert inserted while a client pages never shifts or duplicates rows between pages. Each
filter combination compiles to its own statement, so the planner sees real predicates (a filter
that is absent is not in the SQL at all) rather than ``(:x IS NULL OR col = :x)`` guards that
defeat index use under generic plans.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.storage.models import Alert
from perimeter.storage.zones import latitude, longitude


@dataclass(frozen=True, slots=True)
class AlertRecord:
    id: uuid.UUID
    zone_id: uuid.UUID | None
    zone_name: str
    device_id: str
    kind: str
    lat: float
    lon: float
    occurred_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AlertFilter:
    zone_id: uuid.UUID | None = None
    kinds: Sequence[str] = ()
    device_id: str | None = None
    since: datetime | None = None


def page_query(
    owner_id: uuid.UUID,
    filters: AlertFilter,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> Select[*tuple[Any, ...]]:
    """Newest first; ``after`` continues behind the ``(occurred_at, id)`` of the last alert seen."""
    statement = select(
        Alert.id,
        Alert.zone_id,
        Alert.zone_name,
        Alert.device_id,
        Alert.kind,
        latitude(Alert.position).label("lat"),
        longitude(Alert.position).label("lon"),
        Alert.occurred_at,
        Alert.created_at,
    ).where(Alert.owner_id == owner_id)
    if filters.zone_id is not None:
        statement = statement.where(Alert.zone_id == filters.zone_id)
    if filters.kinds:
        statement = statement.where(Alert.kind.in_(filters.kinds))
    if filters.device_id is not None:
        statement = statement.where(Alert.device_id == filters.device_id)
    if filters.since is not None:
        statement = statement.where(Alert.occurred_at >= filters.since)
    if after is not None:
        statement = statement.where(tuple_(Alert.occurred_at, Alert.id) < tuple_(*after))
    return statement.order_by(Alert.occurred_at.desc(), Alert.id.desc()).limit(limit)


async def page(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    filters: AlertFilter,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[AlertRecord]:
    statement = page_query(owner_id, filters, limit=limit, after=after)
    return [AlertRecord(*row) for row in await conn.execute(statement)]
