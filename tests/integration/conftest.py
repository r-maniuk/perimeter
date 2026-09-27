"""Real PostGIS and NATS for integration tests (containers, or services named by env vars)."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import httpx
import nats
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.app import create_app
from perimeter.bus import topology
from perimeter.bus.publish import StreamPublisher
from perimeter.config import (
    DatabaseSettings,
    EngineSettings,
    NatsSettings,
    ObservabilitySettings,
    SecuritySettings,
    Settings,
    TelemetrySettings,
    load_settings,
)
from perimeter.storage.engine import create_engine
from perimeter.tools.init import migrate


def pinned_base_image(dockerfile: str) -> str:
    """The digest-pinned base image of one of the stack's Dockerfiles: tests run on exactly what
    the stack runs, and an update of that pin reaches them without anyone copying it here."""
    source = (Path(__file__).resolve().parents[2] / dockerfile).read_text()
    found = re.search(r"^FROM\s+(\S+@sha256:[0-9a-f]{64})", source, re.MULTILINE)
    if found is None:
        raise RuntimeError(f"{dockerfile} names no digest-pinned base image")
    return found.group(1)


POSTGIS_IMAGE = pinned_base_image("infra/db/Dockerfile")
NATS_IMAGE = pinned_base_image("infra/nats/Dockerfile")

TABLES = "users, geozones, devices, device_tracks, zone_presence, alerts, outbox, partition_epochs"


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
async def stream(nc: NatsClient) -> AsyncIterator[StreamPublisher]:
    publisher = StreamPublisher(nc)
    yield publisher
    await publisher.close()


@pytest.fixture
def topo() -> topology.Topology:
    """A small, fast topology for tests: 4 partitions, 2-second leases."""
    return topology.Topology(partitions=4, lease_ttl_s=2, sessions_ttl_s=5, revoked_ttl_s=60)


@pytest.fixture
async def provisioned(js: JetStreamContext, topo: topology.Topology) -> topology.Topology:
    await topology.ensure(js, topo)
    return topo


TEST_SESSION_SECRET = "test-session-secret-that-is-long-enough-0123456789"
TEST_INGEST_TOKEN = "test-ingest-token"
TEST_ORIGIN = "http://testserver"


@pytest.fixture
def settings(
    database_settings: DatabaseSettings, nats_settings: NatsSettings, topo: topology.Topology
) -> Settings:
    return load_settings(
        database=database_settings,
        nats=nats_settings,
        security=SecuritySettings(
            session_secret=SecretStr(TEST_SESSION_SECRET),
            ingest_token=SecretStr(TEST_INGEST_TOKEN),
            allowed_origins=TEST_ORIGIN,
        ),
        telemetry=TelemetrySettings(partitions=topo.partitions),
        engine=EngineSettings(lease_ttl_s=topo.lease_ttl_s),
        observability=ObservabilitySettings(perimeter_env="test"),
    )


@pytest.fixture
async def api(
    settings: Settings, provisioned: topology.Topology, db: AsyncEngine
) -> AsyncIterator[FastAPI]:
    """A fully started API replica (lifespan run) against the test services."""
    app = create_app(settings)
    async with LifespanManager(app, startup_timeout=30, shutdown_timeout=30):
        yield app


@pytest.fixture
async def client(api: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url=TEST_ORIGIN) as http:
        yield http
