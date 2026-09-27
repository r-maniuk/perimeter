"""Per-account limits on zones: how many an account keeps, and how fast it may change them."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from perimeter.config import Settings, ZoneSettings, load_settings
from tests.integration.test_api_geozones import auth, zone

MAX_ZONES = 2
WRITES_PER_MINUTE = 6


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return load_settings(
        database=settings.database,
        nats=settings.nats,
        security=settings.security,
        telemetry=settings.telemetry,
        engine=settings.engine,
        observability=settings.observability,
        zones=ZoneSettings(max_per_user=MAX_ZONES, writes_per_minute=WRITES_PER_MINUTE),
    )


async def test_an_account_keeps_a_bounded_number_of_zones(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    first = await client.post("/v1/geozones", json=zone(name="one"), headers=alice)
    assert (await client.post("/v1/geozones", json=zone(name="two"), headers=alice)).is_success
    refused = await client.post("/v1/geozones", json=zone(name="three"), headers=alice)
    assert refused.status_code == 409
    assert refused.json()["code"] == "zone_limit_reached"
    assert refused.json()["limit"] == MAX_ZONES
    # the limit is per account, and deleting a zone makes room again
    bob, _ = await auth(client, "bob")
    assert (await client.post("/v1/geozones", json=zone(), headers=bob)).is_success
    gone = await client.delete(f"/v1/geozones/{first.json()['id']}", headers=alice)
    assert gone.status_code == 204
    assert (await client.post("/v1/geozones", json=zone(name="three"), headers=alice)).is_success


async def test_concurrent_creations_cannot_overshoot_the_limit(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    responses = await asyncio.gather(
        *(client.post("/v1/geozones", json=zone(name=f"z{i}"), headers=alice) for i in range(5))
    )
    assert sorted(r.status_code for r in responses) == [201, 201, 409, 409, 409]
    listed = (await client.get("/v1/geozones", headers=alice)).json()["items"]
    assert len(listed) == MAX_ZONES


async def test_zone_changes_are_rate_limited_per_account(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    created = (await client.post("/v1/geozones", json=zone(), headers=alice)).json()
    path = f"/v1/geozones/{created['id']}"
    for radius in range(301, 301 + WRITES_PER_MINUTE - 1):  # the creation took one token
        assert (await client.patch(path, json={"radius_m": radius}, headers=alice)).is_success
    refused = await client.patch(path, json={"radius_m": 999}, headers=alice)
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1
    # reading is not limited, and another account has its own budget
    assert (await client.get(path, headers=alice)).is_success
    bob, _ = await auth(client, "bob")
    assert (await client.post("/v1/geozones", json=zone(), headers=bob)).is_success
