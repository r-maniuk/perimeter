"""List endpoints page with keyset ranges on their indexes, never with sorts or offsets."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from perimeter.storage import alerts, zones
from perimeter.storage.alerts import AlertFilter


def _nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


async def explain(
    conn: AsyncConnection, statement: Select[*tuple[Any, ...]]
) -> list[dict[str, Any]]:
    """The plan of exactly the SQL the endpoint runs, with its bound parameters."""
    compiled = statement.compile(dialect=conn.dialect, compile_kwargs={"render_postcompile": True})
    parameters = tuple(compiled.params[name] for name in compiled.positiontup or ())
    result = await conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + str(compiled), parameters)
    plan: list[dict[str, Any]] = result.scalar_one()
    return list(_nodes(plan[0]["Plan"]))


AFTER = (datetime(2026, 9, 26, 19, tzinfo=UTC), uuid.UUID(int=7))


@pytest.mark.parametrize(
    ("statement", "table", "index", "key"),
    [
        (
            zones.page_query(uuid.UUID(int=1), limit=51, after=AFTER),
            "geozones",
            "geozones_owner_idx",
            "created_at",
        ),
        (
            alerts.page_query(uuid.UUID(int=1), AlertFilter(), limit=51, after=AFTER),
            "alerts",
            "alerts_owner_id_idx",
            "occurred_at",
        ),
        (
            alerts.page_query(
                uuid.UUID(int=1),
                AlertFilter(
                    zone_id=uuid.UUID(int=2),
                    kinds=["enter", "exit"],
                    device_id="veh-1",
                    since=datetime(2026, 9, 26, tzinfo=UTC),
                ),
                limit=51,
                after=AFTER,
            ),
            "alerts",
            "alerts_owner_id_idx",
            "occurred_at",
        ),
    ],
    ids=["zones", "alerts", "alerts-filtered"],
)
async def test_pages_are_index_range_scans(
    db: AsyncEngine, statement: Select[*tuple[Any, ...]], table: str, index: str, key: str
) -> None:
    async with db.begin() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        nodes = await explain(conn, statement)
    scans = [n for n in nodes if n.get("Relation Name") == table]
    assert [n["Node Type"] for n in scans] == ["Index Scan"], nodes
    assert scans[0]["Index Name"] == index
    assert key in scans[0]["Index Cond"]  # the keyset bound is part of the index range
    assert not [n for n in nodes if n["Node Type"] == "Sort"]
