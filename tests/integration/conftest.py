"""Real PostGIS and NATS for integration tests (containers, or services named by env vars)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from urllib.parse import urlparse

import nats
import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.config import DatabaseSettings, NatsSettings
from perimeter.storage.engine import create_engine
from perimeter.tools.init import migrate

POSTGIS_IMAGE = "imresamu/postgis:18-3.6.1-alpine3.23"
NATS_IMAGE = "nats:2.15.0-alpine"

TABLES = "users, geozones, devices, zone_presence, alerts, outbox, partition_epochs"


def _database_from_url(url: str) -> DatabaseSettings:
    parsed = urlparse(url)
    return DatabaseSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        name=(parsed.path or "/postgres").lstrip("/"),
        user=parsed.username or "postgres",
        password=SecretStr(parsed.password or ""),
        pool_size=10,
    )


@pytest.fixture(scope="session")
def database_settings() -> Iterator[DatabaseSettings]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        settings = _database_from_url(url)
        migrate(settings)
        yield settings
        return
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer(
        POSTGIS_IMAGE, username="perimeter", password="perimeter", dbname="perimeter", driver=None
    ) as container:
        settings = DatabaseSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            name="perimeter",
            user="perimeter",
            password=SecretStr("perimeter"),
            pool_size=10,
        )
        migrate(settings)
        yield settings


@pytest.fixture(scope="session")
def nats_settings() -> Iterator[NatsSettings]:
    url = os.environ.get("TEST_NATS_URL")
    if url:
        yield NatsSettings(url=url)
        return
    from testcontainers.core.container import DockerContainer  # noqa: PLC0415
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy  # noqa: PLC0415

    container = (
        DockerContainer(NATS_IMAGE)
        .with_command("-js")
        .with_exposed_ports(4222)
        .waiting_for(LogMessageWaitStrategy("Server is ready"))
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4222)
        yield NatsSettings(url=f"nats://{host}:{port}")


@pytest.fixture(scope="session")
async def db_engine(database_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_settings, application_name="perimeter-tests")
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """The engine, with every table emptied after the test."""
    yield db_engine
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def nc(nats_settings: NatsSettings) -> AsyncIterator[NatsClient]:
    client = await nats.connect(nats_settings.url, name="perimeter-tests")
    yield client
    js = client.jetstream()
    for info in await js.streams_info():
        if info.config.name:
            with suppress(Exception):
                await js.delete_stream(info.config.name)
    await client.drain()


@pytest.fixture
async def js(nc: NatsClient) -> JetStreamContext:
    return nc.jetstream()


@pytest.fixture
def topo() -> topology.Topology:
    """A small, fast topology for tests: 4 partitions, 2-second leases."""
    return topology.Topology(partitions=4, lease_ttl_s=2, sessions_ttl_s=5, revoked_ttl_s=60)


@pytest.fixture
async def provisioned(js: JetStreamContext, topo: topology.Topology) -> topology.Topology:
    await topology.ensure(js, topo)
    return topo
