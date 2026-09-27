"""Device positions (GeoJSON), one device's state, and trails read back from TELEMETRY."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def auth(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/v1/session", json={"username": username})
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def place(
    db: AsyncEngine, device: str, lon: float, lat: float, *, age_s: float = 5, speed: float = 3.3
) -> None:
    recorded = datetime.now(UTC) - timedelta(seconds=age_s)
    async with db.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO devices (device_id, position, recorded_at, received_at, speed_mps,
                                     heading_deg, accuracy_m)
                VALUES (:d, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :t, :t,
                        :speed, 90, 4.2)
                """
            ),
            {"d": device, "lon": lon, "lat": lat, "t": recorded, "speed": speed},
        )


@pytest.fixture
async def fleet(db: AsyncEngine) -> None:
    await place(db, "ams-1", 4.9041, 52.3676)
    await place(db, "ams-2", 4.8926, 52.3731)
    await place(db, "ams-stale", 4.90, 52.37, age_s=3_600)
    await place(db, "utrecht", 5.1214, 52.0907)
    await place(db, "fiji-east", 179.9, -17.8)
    await place(db, "fiji-west", -179.9, -17.8)
    await place(db, "null-island", 0.0, 0.0)


async def ids(client: httpx.AsyncClient, headers: dict[str, str], **params: Any) -> list[str]:
    response = await client.get("/v1/devices", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return [feature["id"] for feature in response.json()["features"]]


@pytest.mark.usefixtures("fleet")
async def test_viewport_returns_a_feature_collection(client: httpx.AsyncClient) -> None:
    alice = await auth(client, "alice")
    response = await client.get(
        "/v1/devices", params={"bbox": "4.85,52.33,4.95,52.40"}, headers=alice
    )
    collection = response.json()
    assert collection["type"] == "FeatureCollection"
    assert collection["truncated"] is False
    assert [f["id"] for f in collection["features"]] == ["ams-1", "ams-2"]
    feature = collection["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [4.9041, 52.3676]}
    assert feature["properties"]["speed_mps"] == 3.3
    assert feature["properties"]["heading_deg"] == 90
    assert datetime.fromisoformat(feature["properties"]["recorded_at"]).tzinfo is not None


@pytest.mark.usefixtures("fleet")
async def test_viewports_across_the_antimeridian_and_the_whole_world(
    client: httpx.AsyncClient,
) -> None:
    alice = await auth(client, "alice")
    assert await ids(client, alice, bbox="179,-20,-179,-15") == ["fiji-east", "fiji-west"]
    live = ["ams-1", "ams-2", "fiji-east", "fiji-west", "null-island", "utrecht"]
    assert await ids(client, alice, bbox="-180,-90,180,90") == live
    assert await ids(client, alice) == live
    everything = (await client.get("/v1/devices", headers=alice)).json()["features"]
    assert {f["properties"]["speed_mps"] for f in everything} == {3.3}  # float4, no noise


@pytest.mark.usefixtures("fleet")
async def test_limit_marks_the_collection_truncated(client: httpx.AsyncClient) -> None:
    alice = await auth(client, "alice")
    limited = (await client.get("/v1/devices", params={"limit": 2}, headers=alice)).json()
    assert [f["id"] for f in limited["features"]] == ["ams-1", "ams-2"]
    assert limited["truncated"] is True
    boxed = await client.get(
        "/v1/devices", params={"limit": 1, "bbox": "-180,-90,180,90"}, headers=alice
    )
    assert boxed.json()["truncated"] is True


@pytest.mark.parametrize(
    "bbox",
    ["4.8,52.3,4.9", "a,b,c,d", "4.8,52.4,4.9,52.3", "200,0,10,10", "0,-91,1,1", "nan,0,1,1"],
)
async def test_invalid_viewports_are_rejected(client: httpx.AsyncClient, bbox: str) -> None:
    alice = await auth(client, "alice")
    response = await client.get("/v1/devices", params={"bbox": bbox}, headers=alice)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_bbox"


@pytest.mark.usefixtures("fleet")
async def test_device_detail_lists_only_the_callers_zones(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    alice = await auth(client, "alice")
    bob = await auth(client, "bob")
    body = {"name": "Centraal", "center": {"lat": 52.3676, "lon": 4.9041}, "radius_m": 300}
    zone = (await client.post("/v1/geozones", json=body, headers=alice)).json()
    entered = datetime.now(UTC) - timedelta(minutes=3)
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO zone_presence (device_id, zone_id, entered_at, last_seen_at) "
                "VALUES ('ams-1', :z, :e, now())"
            ),
            {"z": uuid.UUID(zone["id"]), "e": entered},
        )
    detail = (await client.get("/v1/devices/ams-1", headers=alice)).json()
    assert detail["type"] == "Feature"
    assert detail["geometry"]["coordinates"] == [4.9041, 52.3676]
    properties = detail["properties"]
    assert (properties["accuracy_m"], properties["speed_mps"]) == (4.2, 3.3)
    assert [(z["name"], z["color"]) for z in properties["zones"]] == [("Centraal", "#6d5dfc")]
    assert datetime.fromisoformat(properties["zones"][0]["entered_at"]) == entered
    assert (await client.get("/v1/devices/ams-1", headers=bob)).json()["properties"]["zones"] == []


async def test_unknown_or_malformed_device_ids(client: httpx.AsyncClient) -> None:
    alice = await auth(client, "alice")
    assert (await client.get("/v1/devices/nobody", headers=alice)).status_code == 404
    assert (await client.get("/v1/devices/nobody/trail", headers=alice)).status_code == 404
    assert (await client.get("/v1/devices/a.b", headers=alice)).status_code == 422
    assert (await client.get("/v1/devices/veh-1%0A", headers=alice)).status_code == 422
    assert (await client.get("/v1/devices", params={"limit": 0}, headers=alice)).status_code == 422


async def report(db: AsyncEngine, device: str, *, ago_s: float, lon: float) -> None:
    """What the engine does with an applied report: keep it in the device's track."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO device_tracks (device_id, recorded_at, position, speed_mps)
                VALUES (:device, now() - make_interval(secs => :ago),
                        ST_SetSRID(ST_MakePoint(:lon, 52.37), 4326)::geography, 4.0)
                """
            ),
            {"device": device, "ago": ago_s, "lon": lon},
        )


async def trail(
    client: httpx.AsyncClient, headers: dict[str, str], device: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(f"/v1/devices/{device}/trail", params=params, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_trail_is_read_back_from_the_track_in_event_time_order(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    alice = await auth(client, "alice")
    await place(db, "veh-1", 4.9, 52.37)
    await place(db, "veh-2", 4.9, 52.37)
    await report(db, "veh-1", ago_s=25 * 60, lon=4.0)  # outside a 15 minute window
    await report(db, "veh-1", ago_s=60, lon=4.8)
    await report(db, "veh-1", ago_s=180, lon=4.7)  # stored late: sorted by device time
    await report(db, "veh-2", ago_s=90, lon=5.5)  # another device
    await report(db, "veh-1", ago_s=10, lon=4.9)
    body = await trail(client, alice, "veh-1")
    assert body["type"] == "Feature"
    assert body["id"] == "veh-1"
    assert body["geometry"]["type"] == "LineString"
    assert [lon for lon, _ in body["geometry"]["coordinates"]] == [4.7, 4.8, 4.9]
    properties = body["properties"]
    stamps = [datetime.fromisoformat(stamp) for stamp in properties["timestamps"]]
    assert stamps == sorted(stamps)
    assert properties["speeds"] == [4.0, 4.0, 4.0]
    assert properties["complete"] is True
    retained = await trail(client, alice, "veh-1", minutes=30)
    assert len(retained["geometry"]["coordinates"]) == 4
    assert retained["properties"]["complete"] is True
    beyond = await trail(client, alice, "veh-1", minutes=45)  # longer than tracks are kept
    assert len(beyond["geometry"]["coordinates"]) == 4
    assert beyond["properties"]["complete"] is False


async def test_trail_geometry_for_one_and_no_points(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    alice = await auth(client, "alice")
    await place(db, "solo", 4.9, 52.37)
    await place(db, "quiet", 4.9, 52.37)
    await report(db, "solo", ago_s=5, lon=4.91)
    single = await trail(client, alice, "solo")
    assert single["geometry"] == {"type": "Point", "coordinates": [4.91, 52.37]}
    assert len(single["properties"]["timestamps"]) == 1
    empty = await trail(client, alice, "quiet")
    assert empty["geometry"] is None
    assert empty["properties"]["timestamps"] == []
    assert empty["properties"]["complete"] is True
