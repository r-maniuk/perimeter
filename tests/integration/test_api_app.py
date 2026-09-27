from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from perimeter.api.security import RevocationList
from perimeter.api.state import AppState
from tests.support import eventually


async def test_liveness_readiness_and_metrics(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200, ready.text
    assert ready.json()["checks"] == {"database": "ok", "broker": "ok", "event_loop": "ok"}
    metrics = await client.get("/metrics/")
    assert metrics.status_code == 200
    assert "perimeter_event_loop_lag_seconds" in metrics.text


async def test_unknown_routes_answer_with_problem_details(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "not_found"
    assert body["type"].endswith("/not_found")


async def test_openapi_document_is_served(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert schema["info"]["title"] == "Perimeter"


async def test_revocations_propagate_between_replicas(api: FastAPI) -> None:
    state: AppState = api.state.perimeter
    other = await RevocationList.open(state.js, keep_s=60)
    try:
        listener = other.subscribe()
        await state.revoked.revoke("token-123")
        await eventually(lambda: other.is_revoked("token-123"))
        assert await asyncio.wait_for(listener.get(), 2) == "token-123"
    finally:
        await other.close()


async def test_the_reference_declares_both_tokens_and_offers_a_fresh_ingest_example(
    client: httpx.AsyncClient,
) -> None:
    document = (await client.get("/openapi.json")).json()
    schemes = document["components"]["securitySchemes"]
    assert schemes["SessionToken"]["scheme"] == "bearer"
    assert schemes["DeviceToken"]["scheme"] == "bearer"
    ingest = document["paths"]["/v1/telemetry"]["post"]
    assert {"DeviceToken": []} in ingest["security"]
    assert {"SessionToken": []} in document["paths"]["/v1/geozones"]["get"]["security"]
    example = ingest["requestBody"]["content"]["application/json"]["example"]["reports"][0]
    stamp = datetime.fromisoformat(example["timestamp"])
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 60
    assert (await client.get("/docs")).status_code == 404  # served by the edge, not the api
