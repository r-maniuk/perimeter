from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.config import DatabaseSettings
from perimeter.tools.init import migrate


async def test_schema_is_complete(db: AsyncEngine) -> None:
    async with db.connect() as conn:
        tables: set[str] = set(
            (
                await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ).scalars()
        )
        indexes: set[str] = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                )
            ).scalars()
        )
        version: str = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    assert {
        "users",
        "geozones",
        "devices",
        "zone_presence",
        "alerts",
        "outbox",
        "partition_epochs",
    } <= tables
    assert {"geozones_active_envelope_gix", "devices_position_gix"} <= indexes
    assert version == "0001"


async def test_envelope_column_is_generated_from_centre_and_radius(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        owner: uuid.UUID = (
            await conn.execute(text("INSERT INTO users (username) VALUES ('gen') RETURNING id"))
        ).scalar_one()
        zone: uuid.UUID = (
            await conn.execute(
                text(
                    "INSERT INTO geozones (owner_id, name, center, radius_m) VALUES (:o, 'z', "
                    "'SRID=4326;POINT(4.9 52.37)'::geography, 1000) RETURNING id"
                ),
                {"o": owner},
            )
        ).scalar_one()
        before: float = (
            await conn.execute(
                text("SELECT ST_XMax(envelope) FROM geozones WHERE id = :z"), {"z": zone}
            )
        ).scalar_one()
        await conn.execute(text("UPDATE geozones SET radius_m = 2000 WHERE id = :z"), {"z": zone})
        after: float = (
            await conn.execute(
                text("SELECT ST_XMax(envelope) FROM geozones WHERE id = :z"), {"z": zone}
            )
        ).scalar_one()
    assert after > before > 4.9


async def test_downgrade_and_upgrade_round_trip(
    db: AsyncEngine, database_settings: DatabaseSettings
) -> None:
    from alembic import command  # noqa: PLC0415

    from perimeter.tools.init import alembic_config  # noqa: PLC0415

    await asyncio.to_thread(command.downgrade, alembic_config(database_settings), "base")
    async with db.connect() as conn:
        remaining: int = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = 'geozones'"
                )
            )
        ).scalar_one()
    assert remaining == 0
    await asyncio.to_thread(migrate, database_settings)
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM geozones"))).scalar_one() == 0
