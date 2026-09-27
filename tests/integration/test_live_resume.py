"""Resume and gap healing: sessions end up with every event of their user exactly once."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.config import Settings
from perimeter.wire import subjects
from perimeter.wire.events import EventType, encode_event, make_event
from tests.integration.live_support import (
    Replica,
    create_user,
    live_client,
    of_type,
    replicas,
    sign_in,
)


@pytest.fixture
async def cluster(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[Replica]]:
    async with replicas(settings, monkeypatch, "api-a", "api-b") as started:
        yield started


async def publish_event(js: JetStreamContext, user_id: uuid.UUID, n: int) -> int:
    event = make_event(EventType.ALERT, {"n": n})
    ack = await js.publish(
        subjects.events(user_id), encode_event(event), headers={"Nats-Msg-Id": event.id}
    )
    return ack.seq


async def signed_in(db: AsyncEngine, settings: Settings, name: str) -> tuple[uuid.UUID, str]:
    user = await create_user(db, name)
    return user, sign_in(settings, user, name).token


async def test_reconnecting_with_resume_after_replays_exactly_the_gap_then_goes_live(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    bob, _ = await signed_in(db, settings, "bob")
    async with live_client(cluster[0].ws(token)) as first:
        await first.expect("hello")
        seen = [await publish_event(js, alice, n) for n in range(2)]
        assert [f["seq"] for f in await first.events(2)] == seen
    missed = []
    for n in range(2, 5):
        missed.append(await publish_event(js, alice, n))
        await publish_event(js, bob, n)  # interleaved: sequences of alice are not contiguous
    async with live_client(cluster[1].ws(token, resume_after=seen[-1])) as second:
        hello = await second.expect("hello")
        assert hello["resume"] == {"mode": "replay", "after": seen[-1]}
        replayed = await second.events(3)
        assert [f["seq"] for f in replayed] == missed
        assert [f["prev"] for f in replayed] == [seen[-1], *missed[:-1]]
        assert [f["event"]["data"]["n"] for f in replayed] == [2, 3, 4]
        live = await publish_event(js, alice, 5)
        [frame] = await second.events(1)
        assert (frame["seq"], frame["prev"]) == (live, missed[-1])
        assert not of_type(await second.quiet(0.3), "event")


async def test_the_resume_point_can_also_come_as_the_first_message(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    seqs = [await publish_event(js, alice, n) for n in range(4)]
    async with live_client(cluster[0].ws(token)) as client:
        await client.send(type="resume", after=seqs[1])
        hello = await client.expect("hello")
        assert hello["resume"] == {"mode": "replay", "after": seqs[1]}
        assert [f["seq"] for f in await client.events(2)] == seqs[2:]


async def test_a_session_started_while_events_flow_misses_nothing_after_its_hello(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    stop = asyncio.Event()
    published: list[int] = []

    async def produce() -> None:
        n = 0
        while not stop.is_set():
            published.append(await publish_event(js, alice, n))
            n += 1
            await asyncio.sleep(0.005)

    producer = asyncio.create_task(produce())
    try:
        await asyncio.sleep(0.2)
        async with live_client(cluster[1].ws(token)) as client:
            hello = await client.expect("hello")
            after = hello["resume"]["after"]
            await asyncio.sleep(0.5)
            stop.set()
            await producer
            expected = [seq for seq in published if seq > after]
            frames = await client.events(len(expected), within=10)
            assert [f["seq"] for f in frames] == expected
            assert not of_type(await client.quiet(0.3), "event")
    finally:
        stop.set()
        await producer


async def test_a_resume_point_older_than_the_stream_resets(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    seqs = [await publish_event(js, alice, n) for n in range(5)]
    await js.purge_stream(subjects.EVENTS_STREAM, seq=seqs[3])  # 0..2 aged out
    async with live_client(cluster[0].ws(token, resume_after=seqs[0])) as client:
        hello = await client.expect("hello")
        assert hello["resume"] == {"mode": "reset", "after": seqs[-1]}
        assert not of_type(await client.quiet(0.3), "event")
        live = await publish_event(js, alice, 9)
        [frame] = await client.events(1)
        assert (frame["seq"], frame["prev"]) == (live, seqs[-1])


@pytest.mark.parametrize("offset", ["foreign", "ahead"])
async def test_a_resume_point_that_is_not_this_users_resets(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings, offset: str
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    bob, _ = await signed_in(db, settings, "bob")
    mine = await publish_event(js, alice, 1)
    foreign = await publish_event(js, bob, 1)
    after = foreign if offset == "foreign" else foreign + 1_000
    async with live_client(cluster[1].ws(token, resume_after=after)) as client:
        hello = await client.expect("hello")
        assert hello["resume"] == {"mode": "reset", "after": mine}


async def test_a_malformed_resume_point_is_a_protocol_error(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    _, token = await signed_in(db, settings, "alice")
    async with live_client(cluster[0].ws(token, resume_after="-3")) as client:
        assert await client.closed() == 1008


async def test_a_replica_that_missed_a_live_copy_heals_the_gap_from_the_stream(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    replica = cluster[0]
    async with live_client(replica.ws(token)) as client:
        await client.expect("hello")
        first = await publish_event(js, alice, 1)
        assert [f["seq"] for f in await client.events(1)] == [first]
        feed = replica.state.hub.feeds.get(alice)
        assert feed is not None
        assert feed.subscription is not None
        await feed.subscription.unsubscribe()  # the replica is deaf for a moment
        lost = [await publish_event(js, alice, n) for n in (2, 3)]
        await feed.subscribe()
        await replica.state.nc.flush()
        after = await publish_event(js, alice, 4)
        frames = await client.events(3)
        assert [f["seq"] for f in frames] == [*lost, after]
        assert [f["prev"] for f in frames] == [first, *lost]
        assert not of_type(await client.quiet(0.3), "event")


async def test_an_audit_delivers_an_event_whose_live_copy_was_lost_and_nothing_followed(
    cluster: list[Replica], db: AsyncEngine, js: JetStreamContext, settings: Settings
) -> None:
    alice, token = await signed_in(db, settings, "alice")
    replica = cluster[1]
    async with live_client(replica.ws(token)) as client:
        await client.expect("hello")
        feed = replica.state.hub.feeds.get(alice)
        assert feed is not None
        assert feed.subscription is not None
        await feed.subscription.unsubscribe()
        lost = await publish_event(js, alice, 1)
        await feed.subscribe()
        await replica.state.hub.feeds.audit()
        [frame] = await client.events(1)
        assert (frame["seq"], frame["prev"]) == (lost, 0)
