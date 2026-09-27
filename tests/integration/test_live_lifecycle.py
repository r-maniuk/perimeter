"""Live sessions at the edges: shutdown, protocol errors, control messages, broker failures."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import nats.errors
import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.live import hub as hub_module
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
from tests.support import eventually

NETHERLANDS = (3.3, 50.7, 7.3, 53.6)


@pytest.fixture
async def cluster(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[Replica]]:
    async with replicas(settings, monkeypatch, "api-a", "api-b") as started:
        yield started


async def signed_in(db: AsyncEngine, settings: Settings, name: str) -> tuple[uuid.UUID, str]:
    user = await create_user(db, name)
    return user, sign_in(settings, user, name).token


async def publish_event(js: JetStreamContext, user_id: uuid.UUID, n: int) -> int:
    event = make_event(EventType.ALERT, {"n": n})
    ack = await js.publish(
        subjects.events(user_id), encode_event(event), headers={"Nats-Msg-Id": event.id}
    )
    return ack.seq


async def test_closing_the_hub_closes_sockets_with_1001_and_refuses_new_ones(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    closing, other = cluster
    async with live_client(closing.ws(token)) as client:
        await client.expect("hello")
        await eventually(lambda: len(other.state.registry.sessions_of(alice)) == 1)
        await closing.state.hub.close()
        assert await client.closed() == 1001
    async with live_client(closing.ws(token)) as late:
        assert await late.closed() == 1001
    await eventually(lambda: other.state.registry.sessions_of(alice) == [])


async def test_a_first_message_that_is_not_a_resume_starts_the_session_at_once(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    _, token = await signed_in(db, settings, "alice")
    async with live_client(cluster[0].ws(token)) as client:
        started = time.perf_counter()
        await client.viewport(*NETHERLANDS)
        hello = await client.expect("hello")
        assert time.perf_counter() - started < hub_module.RESUME_WINDOW_S
        assert hello["resume"]["mode"] == "fresh"
        assert {frame.kind for frame in await client.tiles()} == {FrameKind.SNAPSHOT}


@pytest.mark.parametrize("first", [b"\x00\x01", "not json", '{"type":"teleport"}'])
async def test_a_first_message_outside_the_protocol_closes_with_1008(
    cluster: list[Replica], db: AsyncEngine, settings: Settings, first: str | bytes
) -> None:
    _, token = await signed_in(db, settings, "alice")
    async with live_client(cluster[1].ws(token)) as client:
        await client.ws.send(first)
        assert await client.closed() == 1008
        assert of_type(client.backlog, "hello") == []


async def test_a_resume_in_the_middle_of_a_session_changes_nothing(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    await publish_event(js, alice, 1)
    async with live_client(cluster[0].ws(token)) as client:
        await client.expect("hello")
        await client.send(type="resume", after=0)
        await client.send(type="ping", t=5)
        assert (await client.expect("pong"))["t"] == 5
        assert of_type(await client.quiet(0.3), "event") == []


@pytest.mark.parametrize(
    ("payload", "code", "reason"),
    [
        (b'{"code":4003,"reason":"moved to another account"}', 4003, "moved to another account"),
        (b'{"code":1000}', 4001, "signed out"),  # only application codes are honoured
        (b"garbage", 4001, "signed out"),
        (b"", 4001, "signed out"),
    ],
)
async def test_control_messages_close_exactly_their_session(
    cluster: list[Replica],
    db: AsyncEngine,
    nc: NatsClient,
    settings: Settings,
    payload: bytes,
    code: int,
    reason: str,
) -> None:
    _, token = await signed_in(db, settings, "alice")
    async with (
        live_client(cluster[0].ws(token)) as target,
        live_client(cluster[1].ws(token)) as bystander,
    ):
        sid = (await target.expect("hello"))["session_id"]
        await bystander.expect("hello")
        await nc.publish(subjects.session_control(str(uuid.uuid4())), payload)  # nobody's
        await nc.publish(subjects.session_control(sid), payload)
        await asyncio.wait_for(target.ws.wait_closed(), 5)
        assert (target.ws.close_code, target.ws.close_reason) == (code, reason)
        await bystander.send(type="ping", t=1)
        await bystander.expect("pong")


async def test_frames_on_malformed_position_subjects_are_ignored(
    cluster: list[Replica], db: AsyncEngine, nc: NatsClient, settings: Settings
) -> None:
    _, token = await signed_in(db, settings, "alice")
    replica = cluster[0]
    async with live_client(replica.ws(token)) as client:
        await client.expect("hello")
        await client.viewport(*NETHERLANDS)
        await client.tiles()  # snapshots
        await replica.settle()
        prefix = min(replica.state.hub.tiles.subscribed)
        assert len(prefix) < 12, "a country-wide viewport is covered by wildcard subscriptions"
        leaf = prefix + "0" * (12 - len(prefix))
        tile = tiles.tile_of_quadkey(leaf)
        frame = encode_tile(
            FrameKind.LIVE, 12, tile.x, tile.y, [FramePoint("veh-1", 52.0, 5.0, 1_790_000_000_000)]
        )
        await nc.publish(".".join(("pos", *prefix, "x")), b"not a frame")
        await nc.publish(tiles.subject_for(leaf), frame)
        assert await client.bundle() == [frame]


async def test_a_broker_failure_while_starting_closes_with_1013(
    cluster: list[Replica], db: AsyncEngine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await signed_in(db, settings, "alice")
    replica = cluster[0]

    async def unavailable(*_: Any) -> bool:
        raise nats.errors.TimeoutError

    monkeypatch.setattr(replica.state.registry, "register", unavailable)
    async with live_client(replica.ws(token, resume_after=0)) as client:
        assert await client.closed() == 1013
    monkeypatch.undo()
    async with live_client(replica.ws(token)) as again:
        await again.expect("hello")


async def test_an_unexpected_failure_closes_only_that_socket_with_1011(
    cluster: list[Replica], db: AsyncEngine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    replica = cluster[1]
    async with live_client(replica.ws(token)) as healthy:
        await healthy.expect("hello")

        async def broken(*_: Any) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr(replica.state.hub.feeds, "attach", broken)
        async with live_client(replica.ws(token, resume_after=0)) as failing:
            assert await failing.closed() == 1011
        monkeypatch.undo()
        await eventually(lambda: len(replica.state.registry.sessions_of(alice)) == 1)
        await healthy.send(type="ping", t=2)
        assert (await healthy.expect("pong"))["t"] == 2


@pytest.fixture
async def audited(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Replica]:
    monkeypatch.setattr(hub_module, "AUDIT_INTERVAL_S", 0.2)
    async with replicas(settings, monkeypatch, "api-a") as [started]:
        yield started


async def test_the_periodic_audit_delivers_an_event_whose_live_copy_was_lost(
    audited: Replica, db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    async with live_client(audited.ws(token)) as client:
        await client.expect("hello")
        feed = audited.state.hub.feeds.get(alice)
        assert feed is not None
        assert feed.subscription is not None
        await feed.subscription.unsubscribe()
        lost = await publish_event(js, alice, 1)
        await feed.subscribe()
        [frame] = await client.events(1, within=5)
        assert (frame["seq"], frame["prev"]) == (lost, 0)
        assert frame["event"]["data"] == {"n": 1}
