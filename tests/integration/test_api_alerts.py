"""Alert history: owner scoping, filters, keyset pagination with ties, deleted zones."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

T0 = datetime(2026, 9, 26, 19, 0, tzinfo=UTC)


async def auth(client: httpx.AsyncClient, username: str) -> tuple[dict[str, str], uuid.UUID]:
    response = await client.post("/v1/session", json={"username": username})
    client.cookies.clear()
    session = response.json()
    return {"Authorization": f"Bearer {session['token']}"}, uuid.UUID(session["user"]["id"])


async def make_zone(client: httpx.AsyncClient, headers: dict[str, str], name: str) -> uuid.UUID:
    body = {"name": name, "center": {"lat": 52.37, "lon": 4.89}, "radius_m": 500}
    response = await client.post("/v1/geozones", json=body, headers=headers)
    return uuid.UUID(response.json()["id"])


async def seed(
    db: AsyncEngine,
    owner: uuid.UUID,
    zone: uuid.UUID,
    zone_name: str,
    rows: list[tuple[str, str, datetime]],
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with db.begin() as conn:
        for device, kind, occurred_at in rows:
            ids.append(
                (
                    await conn.execute(
                        text(
                            """
                            INSERT INTO alerts (owner_id, zone_id, zone_name, device_id, kind,
                                                position, occurred_at)
                            VALUES (:owner, :zone, :name, :device, :kind,
                                    ST_SetSRID(ST_MakePoint(4.8931, 52.3729), 4326)::geography,
                                    :at)
                            RETURNING id
                            """
                        ),
                        {
                            "owner": owner,
                            "zone": zone,
                            "name": zone_name,
                            "device": device,
                            "kind": kind,
                            "at": occurred_at,
                        },
                    )
                ).scalar_one()
            )
    return ids


async def fetch(
    client: httpx.AsyncClient, headers: dict[str, str], **params: Any
) -> list[dict[str, Any]]:
    response = await client.get("/v1/alerts", params=params, headers=headers)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


@pytest.fixture
async def world(client: httpx.AsyncClient, db: AsyncEngine) -> dict[str, Any]:
    """Alice with two zones and six alerts, Bob with one zone and one alert."""
    alice, alice_id = await auth(client, "alice")
    bob, bob_id = await auth(client, "bob")
    dam = await make_zone(client, alice, "Dam")
    park = await make_zone(client, alice, "Vondelpark")
    harbour = await make_zone(client, bob, "Harbour")
    rows = [
        ("veh-1", "enter", T0),
        ("veh-1", "dwell", T0 + timedelta(minutes=5)),
        ("veh-1", "exit", T0 + timedelta(minutes=9)),
        ("veh-2", "enter", T0 + timedelta(minutes=2)),
    ]
    dam_ids = await seed(db, alice_id, dam, "Dam", rows)
    park_ids = await seed(
        db,
        alice_id,
        park,
        "Vondelpark",
        [
            ("veh-2", "enter", T0 + timedelta(minutes=7)),
            ("veh-3", "exit", T0 + timedelta(minutes=1)),
        ],
    )
    await seed(db, bob_id, harbour, "Harbour", [("veh-9", "enter", T0 + timedelta(minutes=3))])
    return {"alice": alice, "bob": bob, "dam": dam, "park": park, "ids": dam_ids + park_ids}


async def test_history_is_newest_first_and_private(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    items = await fetch(client, world["alice"])
    assert [(a["device_id"], a["kind"]) for a in items] == [
        ("veh-1", "exit"),
        ("veh-2", "enter"),
        ("veh-1", "dwell"),
        ("veh-2", "enter"),
        ("veh-3", "exit"),
        ("veh-1", "enter"),
    ]
    first = items[0]
    assert first["zone"] == {"id": str(world["dam"]), "name": "Dam"}
    assert first["position"] == {"lat": 52.3729, "lon": 4.8931}
    assert datetime.fromisoformat(first["occurred_at"]) == T0 + timedelta(minutes=9)
    assert {a["id"] for a in items} == {str(i) for i in world["ids"]}
    bob = await fetch(client, world["bob"])
    assert [a["device_id"] for a in bob] == ["veh-9"]


async def test_filters_combine(client: httpx.AsyncClient, world: dict[str, Any]) -> None:
    alice = world["alice"]

    async def devices(**params: Any) -> list[tuple[str, str]]:
        return [(a["device_id"], a["kind"]) for a in await fetch(client, alice, **params)]

    assert await devices(zone_id=str(world["park"])) == [("veh-2", "enter"), ("veh-3", "exit")]
    assert await devices(kind="exit") == [("veh-1", "exit"), ("veh-3", "exit")]
    assert await devices(kind=["enter", "dwell"], device_id="veh-1") == [
        ("veh-1", "dwell"),
        ("veh-1", "enter"),
    ]
    since = (T0 + timedelta(minutes=5)).isoformat()
    assert await devices(since=since) == [("veh-1", "exit"), ("veh-2", "enter"), ("veh-1", "dwell")]
    naive = (T0 + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    assert len(await devices(since=naive)) == 3  # naive means UTC
    assert await devices(zone_id=str(uuid.uuid4())) == []
    assert await devices(device_id="veh-9") == []  # Bob's device, Bob's alert


async def test_pages_split_ties_on_the_same_instant(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    alice, alice_id = await auth(client, "alice")
    zone = await make_zone(client, alice, "Busy")
    ids = await seed(db, alice_id, zone, "Busy", [(f"veh-{i}", "enter", T0) for i in range(5)])
    ids += await seed(db, alice_id, zone, "Busy", [("veh-late", "exit", T0 + timedelta(seconds=1))])
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/v1/alerts", params=params, headers=alice)).json()
        seen += [a["id"] for a in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    tied = sorted((str(i) for i in ids[:5]), reverse=True)  # UUIDv7: id order is creation order
    assert seen == [str(ids[5]), *tied]


async def test_alerts_outlive_their_zone(client: httpx.AsyncClient, world: dict[str, Any]) -> None:
    alice = world["alice"]
    deleted = await client.delete(f"/v1/geozones/{world['park']}", headers=alice)
    assert deleted.status_code == 204
    orphans = [a for a in await fetch(client, alice) if a["zone"]["name"] == "Vondelpark"]
    assert len(orphans) == 2
    assert all(a["zone"]["id"] is None for a in orphans)


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"kind": "teleport"}, 422, "validation_failed"),
        ({"device_id": "veh.1"}, 422, "validation_failed"),
        ({"zone_id": "not-a-uuid"}, 422, "validation_failed"),
        ({"since": "last tuesday"}, 422, "validation_failed"),
        ({"limit": 0}, 422, "validation_failed"),
        ({"limit": 501}, 422, "validation_failed"),
        ({"cursor": "%%%"}, 400, "invalid_cursor"),
    ],
)
async def test_invalid_queries(
    client: httpx.AsyncClient, world: dict[str, Any], params: dict[str, Any], status: int, code: str
) -> None:
    response = await client.get("/v1/alerts", params=params, headers=world["alice"])
    assert response.status_code == status
    assert response.json()["code"] == code


async def test_a_zones_cursor_is_not_an_alerts_cursor(
    client: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    alice = world["alice"]
    zones_page = (await client.get("/v1/geozones", params={"limit": 1}, headers=alice)).json()
    response = await client.get(
        "/v1/alerts", params={"cursor": zones_page["next_cursor"]}, headers=alice
    )
    assert response.status_code == 400
    assert "different list" in response.json()["detail"]
