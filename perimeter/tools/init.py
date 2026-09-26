"""One-shot initialisation: database migrations, then broker topology. Safe to run repeatedly."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config

from perimeter.bus import topology
from perimeter.bus.connection import connect
from perimeter.config import DatabaseSettings, load_settings
from perimeter.ops.logging import configure_logging
from perimeter.storage.engine import database_url

log = structlog.get_logger(__name__)

MIGRATIONS = Path(__file__).resolve().parent.parent / "storage" / "migrations"


def alembic_config(database: DatabaseSettings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["url"] = database_url(database).render_as_string(hide_password=False)
    return config


def migrate(database: DatabaseSettings, revision: str = "head") -> None:
    command.upgrade(alembic_config(database), revision)


async def run() -> None:
    settings = load_settings()
    configure_logging(
        service="init",
        level=settings.observability.log_level,
        json=not settings.is_development,
    )
    log.info("init.migrating", host=settings.database.host, database=settings.database.name)
    await asyncio.to_thread(migrate, settings.database)
    nc = await connect(settings.nats, name="perimeter-init")
    try:
        await topology.ensure(nc.jetstream(), topology.Topology.from_settings(settings))
    finally:
        await nc.drain()
    log.info("init.done")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
