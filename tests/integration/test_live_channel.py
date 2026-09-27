"""The live channel end to end: two real replicas, real PostGIS and NATS, a websockets client."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.config import Settings
from perimeter.domain import tiles
from perimeter.wire import subjects
from perimeter.wire.events import EventType, encode_event, make_event
from perimeter.wire.frames import FrameKind, FramePoint, encode_tile
from tests.integration.live_support import (
    Replica,
    create_user,
    live_client,
    of_type,
    replicas,
    sign_in,
)

AMSTERDAM = (4.9041, 52.3676)
PARIS = (2.3522, 48.8566)
NEAR_AMSTERDAM = (4.8952, 52.3702)


@pytest.fixture
async def cluster(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[Replica]]:
    async with replicas(settings, monkeypatch, "api-a", "api-b") as started:
        yield started


async def seed_device(db: AsyncEngine, device_id: str, lon: float, lat: float, age_s: int) -> None:
    at = datetime.now(UTC) - timedelta(seconds=age_s)
    async with db.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO devices (device_id, position, recorded_at, received_at)
                VALUES (:d, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :t, :t)
                """
            ),
            {"d": device_id, "lon": lon, "lat": lat, "t": at},
        )


def live_frame_for(device_id: str, lon: float, lat: float) -> tuple[str, bytes]:
    tile = tiles.tile_for(lon, lat, 12)
    point = FramePoint(device_id, lat, lon, int(time.time() * 1000), 11.5, 270.0)
    frame = encode_tile(FrameKind.LIVE, tile.z, tile.x, tile.y, [point])
    return tiles.subject_for(tiles.quadkey(tile)), frame


async def publish_event(js: JetStreamContext, user_id: uuid.UUID, n: int) -> int:
    event = make_event(EventType.ALERT, {"n": n, "device_id": f"veh-{n}"})
    ack = await js.publish(
        subjects.events(user_id), encode_event(event), headers={"Nats-Msg-Id": event.id}
    )
    return ack.seq


async def test_hello_comes_first_then_the_session_list(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    token = sign_in(settings, alice, "alice")
    async with live_client(cluster[0].ws(token.token)) as client:
        hello = json.loads(await client.ws.recv())
        assert hello["type"] == "hello"
        assert uuid.UUID(hello["session_id"]).version == 7
        assert hello["user"] == {"id": str(alice), "username": "alice"}
        assert abs(hello["server_time"] - time.time() * 1000) < 5_000
        assert hello["protocol"] == 1
        assert hello["resume"] == {"mode": "fresh", "after": 0}
        assert hello["tile_zoom"] == settings.live.tile_zoom
        assert hello["replica"] == "api-a"
        listed = await client.expect("sessions")
        [me] = listed["sessions"]
        assert me["sid"] == hello["session_id"]
        assert me["current"] is True
        assert me["label"] == "Python websockets"
        assert me["replica"] == "api-a"
        await client.send(type="ping", t=123)
        pong = await client.expect("pong")
        assert pong["t"] == 123
        assert abs(pong["server_time"] - time.time() * 1000) < 5_000


async def test_a_viewport_brings_a_snapshot_then_live_frames_and_nothing_from_elsewhere(
    cluster: list[Replica], db: AsyncEngine, nc: NatsClient, settings: Settings
) -> None:
    await seed_device(db, "veh-1", *AMSTERDAM, age_s=5)
    await seed_device(db, "veh-stale", *NEAR_AMSTERDAM, age_s=3_600)
    await seed_device(db, "veh-paris", *PARIS, age_s=5)
    alice = await create_user(db, "alice")
    token = sign_in(settings, alice, "alice")
    replica = cluster[0]
    async with live_client(replica.ws(token.token)) as client:
        await client.expect("hello")
        await client.viewport(4.88, 52.36, 4.92, 52.375)
        snapshot = await client.tiles()
        assert {frame.kind for frame in snapshot} == {FrameKind.SNAPSHOT}
        assert [p.device_id for frame in snapshot for p in frame.points] == ["veh-1"]
        await replica.settle()
        subject, frame = live_frame_for("veh-2", *NEAR_AMSTERDAM)
        far_subject, far_frame = live_frame_for("veh-far", *PARIS)
        await nc.publish(far_subject, far_frame)
        await nc.publish(subject, frame)
        assert await client.bundle() == [frame]  # forwarded byte for byte
        assert not [item for item in await client.quiet(0.5) if isinstance(item, bytes)]


async def test_an_event_reaches_every_session_of_its_user_once_and_nobody_else(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    bob = await create_user(db, "bob")
    alice_token = sign_in(settings, alice, "alice")
    bob_token = sign_in(settings, bob, "bob")
    async with (
        live_client(cluster[0].ws(alice_token.token)) as on_a,
        live_client(cluster[1].ws(alice_token.token)) as on_b,
        live_client(cluster[1].ws(bob_token.token)) as bob_client,
    ):
        for client in (on_a, on_b, bob_client):
            await client.expect("hello")
        seqs = [await publish_event(js, alice, n) for n in range(3)]
        await publish_event(js, bob, 99)
        for client in (on_a, on_b):
            frames = await client.events(3)
            assert [f["seq"] for f in frames] == seqs
            assert [f["prev"] for f in frames] == [0, *seqs[:-1]]
            assert [f["event"]["data"]["n"] for f in frames] == [0, 1, 2]
            assert not of_type(await client.quiet(0.3), "event")
        [bobs] = await bob_client.events(1)
        assert bobs["event"]["data"]["n"] == 99


async def test_occupancy_pulses_are_forwarded_to_the_users_sessions(
    cluster: list[Replica], db: AsyncEngine, nc: NatsClient, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    bob = await create_user(db, "bob")
    alice_token = sign_in(settings, alice, "alice")
    bob_token = sign_in(settings, bob, "bob")
    pulse = b'{"type":"pulse","window_ms":100,"zones":{"z-1":["veh-1","veh-2"]}}'
    async with (
        live_client(cluster[0].ws(alice_token.token)) as on_a,
        live_client(cluster[1].ws(alice_token.token)) as on_b,
        live_client(cluster[0].ws(bob_token.token)) as bob_client,
    ):
        for client in (on_a, on_b, bob_client):
            await client.expect("hello")
        await nc.publish(subjects.live_pulses(alice), pulse)
        for client in (on_a, on_b):
            assert await client.expect("pulse") == json.loads(pulse)
        assert not of_type(await bob_client.quiet(0.3), "pulse")


async def test_ops_frames_carry_every_live_heartbeat_once_a_second(
    cluster: list[Replica], db: AsyncEngine, nc: NatsClient, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    token = sign_in(settings, alice, "alice")
    engine_beat = {
        "service": "engine",
        "instance": "engine-1",
        "ts": time.time(),
        "partitions": [0, 1],
    }
    async with live_client(cluster[1].ws(token.token)) as client:
        await client.expect("hello")
        assert not of_type(await client.quiet(1.2), "ops")
        await client.send(type="ops", on=True)
        await nc.publish(
            subjects.metrics_heartbeat("engine", "engine-1"), json.dumps(engine_beat).encode()
        )

        def complete(frame: dict[str, object]) -> bool:
            services = frame["services"]
            assert isinstance(services, list)
            names = {(s["service"], s["instance"]) for s in services}
            return {("api", "api-a"), ("api", "api-b"), ("engine", "engine-1")} <= names

        frame = await client.expect("ops", within=5, where=complete)
        api_beat = next(s for s in frame["services"] if s["instance"] == "api-b")
        for key in ("loop_lag_p99_ms", "sessions", "live_out_rate", "live_drops_rate", "ts"):
            assert key in api_beat
        assert api_beat["sessions"] == 1
        first = await client.expect("ops", within=3)
        second = await client.expect("ops", within=3)
        assert 0.5 < second["ts"] - first["ts"] < 1.5
        await client.send(type="ops", on=False)
        await client.quiet(0.2)
        assert not of_type(await client.quiet(1.5), "ops")
