"""Load conditions surface as 503 problems with a retry hint, oversized bodies as 413."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from perimeter.api.state import AppState
from perimeter.config import Settings, load_settings


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """One pooled connection that is given up on quickly, so a test can exhaust the pool."""
    database = settings.database.model_copy(update={"pool_size": 1, "pool_timeout_s": 0.2})
    return load_settings(
        database=database,
        nats=settings.nats,
        security=settings.security,
        telemetry=settings.telemetry,
        engine=settings.engine,
        observability=settings.observability,
    )


@pytest.fixture
async def token(client: httpx.AsyncClient) -> str:
    response = await client.post("/v1/session", json={"username": "alice"})
    client.cookies.clear()
    token: str = response.json()["token"]
    return token


async def test_an_exhausted_pool_answers_503_with_retry_after(
    client: httpx.AsyncClient, api: FastAPI, token: str
) -> None:
    state: AppState = api.state.perimeter
    async with state.db.connect() as held:  # the only connection of the pool
        response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert held is not None
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
    assert response.headers["retry-after"] == "1"
    recovered = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert recovered.status_code == 200


async def test_declared_oversized_bodies_are_refused_before_reading(
    client: httpx.AsyncClient, api: FastAPI
) -> None:
    limit = api.state.settings.ingest.max_body_bytes
    response = await client.post(
        "/v1/session",
        content=b"{" + b" " * limit + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


async def test_streamed_oversized_bodies_are_cut_off(
    client: httpx.AsyncClient, api: FastAPI
) -> None:
    limit = api.state.settings.ingest.max_body_bytes
    sent = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal sent
        chunk = b" " * 65_536
        while sent <= 4 * limit:
            sent += len(chunk)
            yield chunk

    response = await client.post(
        "/v1/session", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert sent < 2 * limit  # reading stopped at the limit instead of buffering it all
