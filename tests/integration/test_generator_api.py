"""The load generator against the real API, for what a stand-in cannot vouch for."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import generator
from perimeter.api.ratelimit import RateLimiter
from tests.integration.conftest import TEST_INGEST_TOKEN
from tests.support import eventually


@pytest.fixture
async def server(api: FastAPI) -> AsyncIterator[str]:
    """The started application behind a real uvicorn server, as in production."""
    config = uvicorn.Config(
        api,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        ws="websockets-sansio",
        log_config=None,
        access_log=False,
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    await eventually(lambda: instance.started, within=10)
    port = instance.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    await asyncio.wait_for(task, 10)


async def test_observe_mode_makes_and_removes_more_zones_than_the_api_allows_at_once(
    server: str, api: FastAPI, db: AsyncEngine
) -> None:
    # Four changes at once, then ten a second: most of the zones below meet a 429 with its
    # Retry-After twice, when they are created and when they are deleted again.
    api.state.zone_limiter = RateLimiter(rate_per_minute=600, burst=4)
    config = generator.Config(
        url=server,
        token=TEST_INGEST_TOKEN,
        devices=20,
        interval=1.0,
        ramp=0.0,
        duration=0.5,
        connections=1,
        batch=20,
        report_every=0.0,
        observe="load-watcher",
        zones=12,
    )
    out, err = io.StringIO(), io.StringIO()
    result = await generator.run(config, out=out, err=err)
    assert result.exit_code == 0, err.getvalue()
    observe = result.summary["observe"]
    assert observe["zones_created"] == observe["zones_deleted"] == 12
    assert "12 of 12 demo zones created in" in out.getvalue()
    assert "12 of 12 demo zones deleted in" in out.getvalue()
    async with db.connect() as conn:
        left: int = (await conn.execute(text("SELECT count(*) FROM geozones"))).scalar_one()
    assert left == 0
