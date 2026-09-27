"""Slow consumers: a client that stops reading is resynced or closed, and nobody else notices."""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.live.connection import LiveConnection
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
    with_live,
)
from tests.support import eventually

AMSTERDAM = (4.9041, 52.3676)
UTRECHT = (5.1214, 52.0907)
BUDGET = 256 * 1024


@pytest.fixture
async def replica(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Replica]:
    tuned = with_live(settings, position_budget_bytes=BUDGET, event_queue_max=16, send_timeout_s=30)
    async with replicas(tuned, monkeypatch, "api-a") as [started]:
        yield started


def area(lon: float, lat: float) -> tuple[float, float, float, float]:
    return lon - 0.01, lat - 0.005, lon + 0.01, lat + 0.005


def heavy_frame(lon: float, lat: float, points: int = 1_000) -> tuple[str, bytes]:
    tile = tiles.tile_for(lon, lat, 12)
    now = int(time.time() * 1000)
    batch = [FramePoint(f"veh-{i:05d}", lat, lon, now) for i in range(points)]
    subject = tiles.subject_for(tiles.quadkey(tile))
    return subject, encode_tile(FrameKind.LIVE, tile.z, tile.x, tile.y, batch)


def probe_frame(n: int) -> tuple[str, bytes]:
    tile = tiles.tile_for(*UTRECHT, 12)
    point = FramePoint(f"probe-{n}", UTRECHT[1], UTRECHT[0], time.time_ns() // 1_000_000)
    subject = tiles.subject_for(tiles.quadkey(tile))
    return subject, encode_tile(FrameKind.LIVE, tile.z, tile.x, tile.y, [point])


async def flood_until(
    nc: NatsClient, subject: str, frame: bytes, condition: object, *, limit: int = 4_000
) -> int:
    """Publish ``frame`` until ``condition()`` holds; how many frames that took."""
    assert callable(condition)
    for sent in range(1, limit + 1):
        await nc.publish(subject, frame)
        if sent % 20 == 0:
            await nc.flush()
            await asyncio.sleep(0.005)
            if condition():
                return sent
    msg = f"condition not reached after {limit} frames"
    raise AssertionError(msg)


async def signed_in(db: AsyncEngine, settings: Settings, name: str) -> tuple[uuid.UUID, str]:
    user = await create_user(db, name)
    return user, sign_in(settings, user, name).token


def connection_of(replica: Replica, sid: str) -> LiveConnection:
    session = replica.state.hub.session(sid)
    assert session is not None
    return session.conn


async def test_a_client_that_stops_reading_positions_is_resynced_while_others_stay_fast(
    replica: Replica, db: AsyncEngine, nc: NatsClient, settings: Settings
) -> None:
    _, slow_token = await signed_in(db, settings, "slow")
    _, fast_token = await signed_in(db, settings, "fast")
    async with (
        live_client(replica.ws(slow_token), max_queue=1) as slow,
        live_client(replica.ws(fast_token)) as fast,
    ):
        sid = (await slow.expect("hello"))["session_id"]
        await fast.expect("hello")
        await slow.viewport(*area(*AMSTERDAM))
        await fast.viewport(*area(*UTRECHT))
        await slow.tiles()  # the (empty) snapshot; from here on the slow client stops reading
        await fast.tiles()
        await replica.settle()
        conn = connection_of(replica, sid)
        latencies: list[float] = []
        probing = asyncio.Event()
        probing.set()

        async def probe() -> None:
            n = 0
            while probing.is_set():
                subject, frame = probe_frame(n)
                started = time.perf_counter()
                await nc.publish(subject, frame)
                [received] = await fast.tiles(within=2)
                assert received.points[0].device_id == f"probe-{n}"
                latencies.append(time.perf_counter() - started)
                n += 1
                await asyncio.sleep(0.01)

        prober = asyncio.create_task(probe())
        subject, frame = heavy_frame(*AMSTERDAM)
        sent = await flood_until(nc, subject, frame, lambda: conn.positions_paused)
        loop = asyncio.get_running_loop()
        stalled_until = loop.time() + 1.5  # keep flooding the stalled client meanwhile
        while loop.time() < stalled_until:
            await nc.publish(subject, frame)
            sent += 1
            await asyncio.sleep(0.005)
        probing.clear()
        await prober
        assert not conn.closing, "a slow position reader is resynced, not disconnected"
        assert len(latencies) >= 20
        assert statistics.median(latencies) < 0.05
        assert max(latencies) < 0.5
        items = await slow.quiet(1.0)  # reading again: the backlog drains, then the resync
        resyncs = of_type(items, "resync")
        assert resyncs == [{"type": "resync", "scope": "positions"}]
        delivered = sum(1 for i in items if isinstance(i, bytes))
        assert 0 < delivered < sent
        await slow.viewport(*area(*AMSTERDAM))
        assert {f.kind for f in await slow.tiles()} == {FrameKind.SNAPSHOT}
        await nc.publish(subject, frame)
        assert {f.kind for f in await slow.tiles()} == {FrameKind.LIVE}


async def test_a_client_that_falls_behind_its_events_gets_4008_and_resumes_the_rest(
    replica: Replica, db: AsyncEngine, nc: NatsClient, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    async with live_client(replica.ws(token), max_queue=1) as slow:
        hello = await slow.expect("hello")
        await slow.viewport(*area(*AMSTERDAM))
        await slow.tiles()
        await replica.settle()
        conn = connection_of(replica, hello["session_id"])
        subject, frame = heavy_frame(*AMSTERDAM)
        await flood_until(nc, subject, frame, lambda: conn.positions_paused)  # socket stuck
        published = []
        for n in range(24):
            event = make_event(EventType.ALERT, {"n": n})
            ack = await js.publish(
                subjects.events(alice), encode_event(event), headers={"Nats-Msg-Id": event.id}
            )
            published.append(ack.seq)
        await eventually(lambda: conn.closing)
        assert conn.close_code == 4008
        assert await slow.closed(within=5) == 4008
        got = [frame["seq"] for frame in of_type(slow.backlog, "event")]
    last = got[-1] if got else hello["resume"]["after"]
    async with live_client(replica.ws(token, resume_after=last)) as again:
        resumed = await again.expect("hello")
        assert resumed["resume"]["mode"] == "replay"
        rest = await again.events(len(published) - len(got))
        assert got + [f["seq"] for f in rest] == published
