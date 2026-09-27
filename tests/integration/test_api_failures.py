"""Failure paths: deadlock victims are retried, outages are 503s, bugs stay 500s."""

from __future__ import annotations

from typing import Any

import httpx
import nats.errors
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import DBAPIError, IntegrityError

from perimeter.api.state import AppState
from perimeter.storage import users, zones


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


async def auth(client: httpx.AsyncClient, username: str = "alice") -> dict[str, str]:
    response = await client.post("/v1/session", json={"username": username})
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def new_zone(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    body = {"name": "Dam", "center": {"lat": 52.37, "lon": 4.89}, "radius_m": 200}
    zone_id: str = (await client.post("/v1/geozones", json=body, headers=headers)).json()["id"]
    return zone_id


async def test_a_deadlock_victim_is_retried_transparently(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = await auth(client)
    zone_id = await new_zone(client, alice)
    original = zones.get
    calls = 0

    async def deadlocked_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DBAPIError("SELECT", {}, _DriverError("40P01"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(zones, "get", deadlocked_once)
    response = await client.patch(
        f"/v1/geozones/{zone_id}", json={"is_active": False}, headers=alice
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert calls == 2


async def test_a_persistent_deadlock_is_a_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = await auth(client)
    zone_id = await new_zone(client, alice)

    async def always_deadlocked(*args: Any, **kwargs: Any) -> Any:
        raise DBAPIError("SELECT", {}, _DriverError("40P01"))

    monkeypatch.setattr(zones, "get", always_deadlocked)
    response = await client.delete(f"/v1/geozones/{zone_id}", headers=alice)
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"


async def test_programming_errors_stay_internal_errors(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = await auth(client)

    async def check_violation(*args: Any, **kwargs: Any) -> Any:
        raise IntegrityError("INSERT", {}, _DriverError("23514"))

    async def unique_violation(*args: Any, **kwargs: Any) -> Any:
        raise DBAPIError("SELECT", {}, _DriverError("23505"))

    monkeypatch.setattr(zones, "create", check_violation)
    body = {"name": "Dam", "center": {"lat": 52.37, "lon": 4.89}, "radius_m": 200}
    created = await client.post("/v1/geozones", json=body, headers=alice)
    assert created.status_code == 500
    assert created.json()["code"] == "internal"
    monkeypatch.setattr(users, "get", unique_violation)
    me = await client.get("/v1/me", headers=alice)
    assert me.status_code == 500


async def test_sign_out_fails_loudly_when_it_cannot_be_recorded(
    client: httpx.AsyncClient, api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = await auth(client)
    state: AppState = api.state.perimeter

    async def broker_down(token_id: str) -> None:
        raise nats.errors.TimeoutError

    monkeypatch.setattr(state.revoked, "revoke", broker_down)
    response = await client.delete("/v1/session", headers=alice)
    assert response.status_code == 503
    assert response.json()["code"] == "revocation_unavailable"
    assert response.headers["retry-after"] == "1"


async def test_signing_out_with_a_broken_token_just_clears_the_cookie(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/v1/session", headers={"Authorization": "Bearer broken"})
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_the_openapi_document_describes_every_endpoint(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    expected = {
        "/v1/session": {"post", "delete"},
        "/v1/me": {"get"},
        "/v1/geozones": {"get", "post"},
        "/v1/geozones/{zone_id}": {"get", "patch", "delete"},
        "/v1/geozones/{zone_id}/occupants": {"get"},
        "/v1/alerts": {"get"},
        "/v1/devices": {"get"},
        "/v1/devices/{device_id}": {"get"},
        "/v1/devices/{device_id}/trail": {"get"},
        "/v1/telemetry": {"post"},
    }
    for path, methods in expected.items():
        assert methods <= set(paths[path]), path
    ingest = paths["/v1/telemetry"]["post"]
    body = ingest["requestBody"]["content"]
    assert {"application/json", "application/msgpack"} <= set(body)
    assert len(body["application/json"]["schema"]["oneOf"]) == 3
    assert "202" in ingest["responses"]
    assert "IngestResult" in schema["components"]["schemas"]
    assert schema["components"]["schemas"]["ZoneCreate"]["examples"]
