"""Alembic environment: async engine, one migrator at a time (transaction advisory lock)."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from perimeter.config import DatabaseSettings
from perimeter.storage.engine import database_url
from perimeter.storage.models import Base

MIGRATION_LOCK_KEY = 0x70657269  # "peri"

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url")
    if url is None:
        url = database_url(DatabaseSettings()).render_as_string(hide_password=False)
    return str(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    # Transaction-scoped: released on commit or rollback, so a failed migration cannot leak it.
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async())
