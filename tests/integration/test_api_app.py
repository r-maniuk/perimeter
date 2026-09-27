from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from perimeter.api.security import RevocationList
from perimeter.api.state import AppState
from tests.support import eventually

# Escapes that Python's re reads as anchors, and JavaScript as the letters themselves.
PYTHON_ONLY_ANCHORS = re.compile(r"\\[AZz]")


def patterns(node: object) -> Iterator[str]:
    """Every ``pattern`` in (a part of) an OpenAPI document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from patterns(value)
    elif isinstance(node, list):
        for value in node:
            yield from patterns(value)


def as_javascript_reads_it(pattern: str) -> str:
    """``pattern`` the way a JavaScript ``RegExp`` reads it, where the two languages differ here."""
    return PYTHON_ONLY_ANCHORS.sub(lambda anchor: anchor[0][1], pattern)


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


async def test_every_pattern_in_the_reference_means_the_same_in_javascript(
    client: httpx.AsyncClient,
) -> None:
    # The reference's "Try it out" refuses to send a value its pattern rejects, and so does any
    # generated client: a pattern that only Python reads right locks every such client out.
    document = (await client.get("/openapi.json")).json()
    assert [p for p in patterns(document) if PYTHON_ONLY_ANCHORS.search(p)] == []
    paths = document["paths"]
    device_ids = {
        path: list(patterns(paths[path]))
        for path in (
            "/v1/devices/{device_id}",
            "/v1/devices/{device_id}/trail",
            "/v1/alerts",
            "/v1/telemetry",
        )
    }
    assert all(device_ids.values())  # every one of them describes the device id with a pattern
    for published in device_ids.values():
        for pattern in published:
            assert re.search(as_javascript_reads_it(pattern), "dev-00001"), pattern
