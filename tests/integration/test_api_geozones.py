"""Geozone CRUD, validation, isolation, optimistic concurrency, pagination, presence and events."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.wire import subjects
from tests.support import eventually

DAM = {"lat": 52.3731, "lon": 4.8926}


async def auth(client: httpx.AsyncClient, username: str) -> tuple[dict[str, str], str]:
    response = await client.post("/v1/token", json={"username": username})
    session = response.json()
    return {"Authorization": f"Bearer {session['token']}"}, session["user"]["id"]


def zone(**overrides: Any) -> dict[str, Any]:
    return {"name": "Dam Square", "center": DAM, "radius_m": 250, **overrides}


async def create(client: httpx.AsyncClient, headers: dict[str, str], **fields: Any) -> Any:
    response = await client.post("/v1/geozones", json=zone(**fields), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_zone_lifecycle(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    response = await client.post(
        "/v1/geozones", json=zone(color="#AABBCC", dwell_s=300), headers=alice
    )
    assert response.status_code == 201
    created = response.json()
    assert response.headers["location"] == f"/v1/geozones/{created['id']}"
    assert response.headers["etag"] == '"v1"'
    assert created["center"] == DAM  # exact round trip through geography
    assert (created["color"], created["dwell_s"], created["version"]) == ("#aabbcc", 300, 1)
    assert (created["is_active"], created["notify_enter"], created["occupancy"]) == (True, True, 0)

    fetched = await client.get(f"/v1/geozones/{created['id']}", headers=alice)
    assert fetched.json() == created
    assert fetched.headers["etag"] == '"v1"'

    patched = await client.patch(
        f"/v1/geozones/{created['id']}",
        json={"radius_m": 400, "center": {"lat": 52.37, "lon": 4.9}, "dwell_s": None},
        headers=alice,
    )
    assert patched.status_code == 200
    body = patched.json()
    assert patched.headers["etag"] == '"v2"'
    assert (body["radius_m"], body["center"], body["dwell_s"]) == (
        400,
        {"lat": 52.37, "lon": 4.9},
        None,
    )
    assert body["name"] == "Dam Square"
    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(
        created["updated_at"]
    )

    deleted = await client.delete(f"/v1/geozones/{created['id']}", headers=alice)
    assert deleted.status_code == 204
    missing = await client.get(f"/v1/geozones/{created['id']}", headers=alice)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_a_token_that_outlived_its_account_cannot_create_zones(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    ghost, _ = await auth(client, "ghost")
    async with db.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE username = 'ghost'"))
    response = await client.post("/v1/geozones", json=zone(), headers=ghost)
    assert response.status_code == 401
    assert "no longer exists" in response.json()["detail"]


async def test_a_patch_that_changes_nothing_keeps_the_version(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    created = await create(client, alice)
    same = await client.patch(
        f"/v1/geozones/{created['id']}",
        json={"name": "  Dam Square ", "radius_m": 250.0, "center": DAM},
        headers=alice,
    )
    assert same.status_code == 200
    assert same.json()["version"] == 1
    empty = await client.patch(f"/v1/geozones/{created['id']}", json={}, headers=alice)
    assert empty.json()["version"] == 1


@pytest.mark.parametrize(
    "body",
    [
        zone(radius_m=9.99),
        zone(radius_m=100_000.1),
        zone(name=""),
        zone(name="   "),
        zone(name="x" * 81),
        zone(name="line\nbreak"),
        zone(color="violet"),
        zone(color="#12345"),
        zone(center={"lat": 90.5, "lon": 0}),
        zone(center={"lat": 0, "lon": -181}),
        zone(center={"lat": 0}),
        zone(dwell_s=9),
        zone(dwell_s=86_401),
        zone(is_active="sometimes"),
        zone(extra="field"),
        {"name": "no centre", "radius_m": 100},
    ],
)
async def test_invalid_zones_are_rejected(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    alice, _ = await auth(client, "alice")
    response = await client.post("/v1/geozones", json=body, headers=alice)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"


@pytest.mark.parametrize(
    "patch",
    [{"name": None}, {"radius_m": None}, {"center": None}, {"radius_m": 5}, {"unknown": 1}],
)
async def test_invalid_patches_are_rejected(
    client: httpx.AsyncClient, patch: dict[str, Any]
) -> None:
    alice, _ = await auth(client, "alice")
    created = await create(client, alice)
    response = await client.patch(f"/v1/geozones/{created['id']}", json=patch, headers=alice)
    assert response.status_code == 422


async def test_zones_are_private_to_their_owner(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    bob, _ = await auth(client, "bob")
    created = await create(client, alice)
    path = f"/v1/geozones/{created['id']}"
    assert (await client.get(path, headers=bob)).status_code == 404
    assert (await client.patch(path, json={"radius_m": 500}, headers=bob)).status_code == 404
    assert (await client.delete(path, headers=bob)).status_code == 404
    assert (await client.get(f"{path}/occupants", headers=bob)).status_code == 404
    assert (await client.get("/v1/geozones", headers=bob)).json()["items"] == []
    assert (await client.get(path, headers=alice)).json()["radius_m"] == 250
    assert (await client.get(f"/v1/geozones/{uuid.uuid4()}", headers=alice)).status_code == 404
    assert (await client.get("/v1/geozones")).status_code == 401


async def test_if_match_protects_against_lost_updates(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    created = await create(client, alice)
    path = f"/v1/geozones/{created['id']}"
    first = await client.patch(path, json={"radius_m": 300}, headers={**alice, "If-Match": '"v1"'})
    assert first.status_code == 200
    stale = await client.patch(path, json={"radius_m": 350}, headers={**alice, "If-Match": '"v1"'})
    assert stale.status_code == 412
    assert stale.json()["code"] == "precondition_failed"
    assert stale.json()["current_version"] == 2
    assert stale.headers["etag"] == '"v2"'
    weak = await client.patch(path, json={"radius_m": 350}, headers={**alice, "If-Match": 'W/"v2"'})
    assert weak.status_code == 412
    listed = await client.patch(
        path, json={"radius_m": 350}, headers={**alice, "If-Match": '"v7", "v2"'}
    )
    assert listed.status_code == 200
    assert (await client.get(path, headers=alice)).json()["radius_m"] == 350
    assert (await client.delete(path, headers={**alice, "If-Match": '"v1"'})).status_code == 412
    assert (await client.delete(path, headers={**alice, "If-Match": "*"})).status_code == 204


async def test_keyset_pagination_walks_every_zone_once(client: httpx.AsyncClient) -> None:
    alice, _ = await auth(client, "alice")
    ids = [(await create(client, alice, name=f"zone {i}"))["id"] for i in range(7)]
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/v1/geozones", params=params, headers=alice)).json()
        seen += [item["id"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == list(reversed(ids))  # newest first, each exactly once


@pytest.mark.parametrize("cursor", ["garbage", "AAAAAAAA"])
async def test_foreign_or_broken_cursors_are_rejected(
    client: httpx.AsyncClient, cursor: str
) -> None:
    alice, _ = await auth(client, "alice")
    response = await client.get("/v1/geozones", params={"cursor": cursor}, headers=alice)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


async def seed_presence(
    db: AsyncEngine, zone_id: str, devices: dict[str, int], *, now: datetime
) -> None:
    """Devices at the zone centre with presence rows ``entered`` seconds ago."""
    async with db.begin() as conn:
        for device, entered_ago in devices.items():
            await conn.execute(
                text(
                    """
                    INSERT INTO devices (device_id, position, recorded_at, received_at, speed_mps)
                    VALUES (:d, ST_SetSRID(ST_MakePoint(4.8926, 52.3731), 4326)::geography,
                            :t, :t, 8.1)
                    ON CONFLICT (device_id) DO NOTHING
                    """
                ),
                {"d": device, "t": now},
            )
            await conn.execute(
                text(
                    "INSERT INTO zone_presence (device_id, zone_id, entered_at, last_seen_at) "
                    "VALUES (:d, :z, :e, :t)"
                ),
                {
                    "d": device,
                    "z": uuid.UUID(zone_id),
                    "e": now - timedelta(seconds=entered_ago),
                    "t": now,
                },
            )


async def presence_count(db: AsyncEngine, zone_id: str) -> int:
    async with db.connect() as conn:
        count: int = (
            await conn.execute(
                text("SELECT count(*) FROM zone_presence WHERE zone_id = :z"),
                {"z": uuid.UUID(zone_id)},
            )
        ).scalar_one()
    return count


async def test_occupancy_and_occupants(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    alice, _ = await auth(client, "alice")
    full = await create(client, alice, name="full")
    empty = await create(client, alice, name="empty")
    now = datetime.now(UTC)
    await seed_presence(db, full["id"], {"veh-1": 300, "veh-2": 10, "veh-3": 60}, now=now)
    listed = (await client.get("/v1/geozones", headers=alice)).json()["items"]
    assert {z["name"]: z["occupancy"] for z in listed} == {"full": 3, "empty": 0}
    assert (await client.get(f"/v1/geozones/{full['id']}", headers=alice)).json()["occupancy"] == 3
    occupants = (
        await client.get(f"/v1/geozones/{full['id']}/occupants", params={"limit": 2}, headers=alice)
    ).json()
    assert occupants["zone_id"] == full["id"]
    assert occupants["occupancy"] == 3
    assert [o["device_id"] for o in occupants["items"]] == ["veh-2", "veh-3"]
    assert occupants["items"][0]["position"] == DAM
    assert occupants["items"][0]["speed_mps"] == 8.1  # float4 column, no binary noise
    none = await client.get(f"/v1/geozones/{empty['id']}/occupants", headers=alice)
    assert none.json()["items"] == []


async def test_deactivation_forgets_presence_but_geometry_edits_keep_it(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    alice, _ = await auth(client, "alice")
    created = await create(client, alice)
    path = f"/v1/geozones/{created['id']}"
    await seed_presence(db, created["id"], {"veh-1": 5, "veh-2": 5}, now=datetime.now(UTC))
    moved = await client.patch(path, json={"radius_m": 900}, headers=alice)
    assert moved.json()["occupancy"] == 2
    assert await presence_count(db, created["id"]) == 2
    paused = await client.patch(path, json={"is_active": False}, headers=alice)
    assert (paused.json()["is_active"], paused.json()["occupancy"]) == (False, 0)
    assert await presence_count(db, created["id"]) == 0
    resumed = await client.patch(path, json={"is_active": True}, headers=alice)
    assert resumed.json()["occupancy"] == 0


async def test_deactivation_waits_for_a_batch_that_saw_the_zone_active(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    # An engine batch reads the rules of its zones FOR KEY SHARE and writes presence for them
    # before it commits; a zone deactivated in between must not keep that presence.
    alice, _ = await auth(client, "alice")
    created = await create(client, alice)
    zone_id = uuid.UUID(created["id"])
    async with db.connect() as batch, batch.begin():
        await batch.execute(
            text("SELECT id FROM geozones WHERE id = :z FOR KEY SHARE"), {"z": zone_id}
        )
        pausing = asyncio.create_task(
            client.patch(f"/v1/geozones/{zone_id}", json={"is_active": False}, headers=alice)
        )
        await asyncio.sleep(0.3)
        assert not pausing.done()  # it waits for the batch
        await batch.execute(
            text(
                "INSERT INTO zone_presence (device_id, zone_id, entered_at, last_seen_at) "
                "VALUES ('veh-1', :z, now(), now())"
            ),
            {"z": zone_id},
        )
    paused = await pausing
    assert paused.status_code == 200
    assert paused.json()["occupancy"] == 0
    assert await presence_count(db, created["id"]) == 0


async def test_every_change_is_published_as_an_event_with_its_sequence(
    client: httpx.AsyncClient, nc: NatsClient, js: JetStreamContext, db: AsyncEngine
) -> None:
    alice, alice_id = await auth(client, "alice")
    received: list[Msg] = []

    async def collect(msg: Msg) -> None:
        received.append(msg)

    await nc.subscribe(subjects.live_events(alice_id), cb=collect)
    await nc.flush()
    created = await create(client, alice)
    path = f"/v1/geozones/{created['id']}"
    updated = (await client.patch(path, json={"name": "Dam"}, headers=alice)).json()
    await client.patch(path, json={"name": "Dam"}, headers=alice)  # no change: no event
    await client.delete(path, headers=alice)
    await eventually(lambda: len(received) == 3)
    await asyncio.sleep(0.1)
    assert len(received) == 3
    events = [json.loads(msg.data) for msg in received]
    assert [e["type"] for e in events] == ["zone.created", "zone.updated", "zone.deleted"]
    assert events[0]["data"] == created
    assert events[1]["data"] == updated
    assert events[2]["data"] == {"id": created["id"]}
    headers = [msg.headers or {} for msg in received]
    sequences = [int(h["Nats-Sequence"]) for h in headers]
    assert sequences == sorted(sequences)
    assert [int(h["Nats-Last-Sequence"]) for h in headers] == [0, *sequences[:-1]]
    async with db.connect() as conn:  # the fast path relayed and cleared every outbox row
        assert (await conn.execute(text("SELECT count(*) FROM outbox"))).scalar_one() == 0
    stored = await js.stream_info(subjects.EVENTS_STREAM)
    assert stored.state.messages == 3
