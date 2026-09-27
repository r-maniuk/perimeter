from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg

from perimeter.api.live.events import (
    REPLAY_MAX_EVENTS,
    EventSink,
    EventStore,
    EventStoreError,
    FeedMember,
    StoredEvent,
    UserFeed,
    catch_up,
    plan_resume,
    replay_frames,
)
from perimeter.api.live.protocol import ResumeMode
from perimeter.wire import subjects

USER = uuid.UUID(int=7)
OTHER = uuid.UUID(int=8)


def frame(seq: int) -> bytes:
    return b"frame-%d" % seq


class FakeStore:
    """An EVENTS stream in memory: ``events`` maps sequence -> (subject, payload)."""

    def __init__(self, events: dict[int, tuple[str, bytes]] | None = None) -> None:
        self.events = events or {}
        self.first = min(self.events, default=1)
        self.reads: list[tuple[int, int | None]] = []
        self.fail = False

    def add(self, seq: int, user: uuid.UUID = USER) -> None:
        self.events[seq] = (subjects.events(user), b'{"n":%d}' % seq)

    async def last_seq(self, user_id: uuid.UUID) -> int:
        mine = [s for s, (subject, _) in self.events.items() if subject == subjects.events(user_id)]
        return max(mine, default=0)

    async def bounds(self) -> tuple[int, int]:
        return self.first, max(self.events, default=max(self.first - 1, 0))

    async def subject_at(self, seq: int) -> str | None:
        found = self.events.get(seq)
        return found[0] if found and seq >= self.first else None

    async def count_after(self, user_id: uuid.UUID, after: int) -> int:
        return len([e async for e in self.read(user_id, after=after)])

    async def read(
        self, user_id: uuid.UUID, *, after: int, until: int | None = None
    ) -> AsyncIterator[StoredEvent]:
        self.reads.append((after, until))
        if self.fail:
            raise EventStoreError("down")
        for seq in sorted(self.events):
            subject, payload = self.events[seq]
            wanted = seq > after and (until is None or seq <= until) and seq >= self.first
            if wanted and subject == subjects.events(user_id):
                yield StoredEvent(seq, payload)


def as_store(store: FakeStore) -> EventStore:
    return cast("EventStore", store)


def test_a_sink_drops_what_the_client_has_and_buffers_while_catching_up() -> None:
    delivered: list[bytes] = []
    sink = EventSink(delivered.append, last_seq=5, buffer_max=3)
    sink.offer(5, frame(5))
    sink.offer(7, frame(7))
    assert sink.buffered == 1
    assert delivered == []
    sink.catching_up = False
    sink.offer(6, frame(6))  # newer than last_seq (5): a live sink delivers whatever is newer
    sink.offer(6, frame(6))
    assert delivered == [frame(6)]


def test_an_overflowing_catch_up_buffer_is_dropped_and_flagged() -> None:
    sink = EventSink(lambda _: None, buffer_max=2)
    for seq in (1, 2, 3, 4):
        sink.offer(seq, frame(seq))
    assert sink.overflowed
    assert sink.buffered == 0
    sink.restart_buffer()
    assert not sink.overflowed


def reader(store: FakeStore) -> Callable[[int, int], AsyncIterator[tuple[int, bytes]]]:
    def read(after: int, until: int) -> AsyncIterator[tuple[int, bytes]]:
        return replay_frames(as_store(store), USER, after, until)

    return read


async def collect(sink_frames: list[bytes]) -> Callable[[bytes], Awaitable[None]]:
    async def put(item: bytes) -> None:
        sink_frames.append(item)
        await asyncio.sleep(0)

    return put


async def test_catch_up_replays_the_gap_then_flushes_newer_live_frames_then_goes_live() -> None:
    store = FakeStore()
    for seq in (3, 5, 8):
        store.add(seq)
    out: list[bytes] = []
    sink = EventSink(out.append, last_seq=2, buffer_max=16)
    latest = 8
    sink.offer(8, b"live-8")  # arrived live while the session was starting
    sink.offer(11, b"live-11")
    replayed = await catch_up(
        sink, latest=lambda: latest, read=reader(store), put=await collect(out)
    )
    assert replayed == 3
    assert [json.loads(f)["seq"] for f in out[:3]] == [3, 5, 8]
    assert [json.loads(f)["prev"] for f in out[:3]] == [2, 3, 5]
    assert out[3:] == [b"live-11"]
    assert not sink.catching_up
    sink.offer(12, b"live-12")
    sink.offer(11, b"live-11")
    assert out[-1] == b"live-12"
    assert sink.last_seq == 12


async def test_catch_up_reads_again_what_an_overflowing_buffer_lost() -> None:
    store = FakeStore()
    for seq in range(1, 21):
        store.add(seq)
    out: list[bytes] = []
    sink = EventSink(out.append, last_seq=0, buffer_max=2)
    feed = {"last": 10}

    async def put(item: bytes) -> None:
        out.append(item)
        if len(out) == 3:  # while replay is waiting for the socket, a burst arrives live
            for seq in range(11, 21):
                feed["last"] = seq
                sink.offer(seq, b"live")
        await asyncio.sleep(0)

    await catch_up(sink, latest=lambda: feed["last"], read=reader(store), put=put)
    assert [json.loads(f)["seq"] for f in out] == list(range(1, 21))
    assert store.reads == [(0, 10), (10, 20)]
    assert not sink.catching_up


async def test_catch_up_without_a_gap_only_flushes_the_buffer() -> None:
    store = FakeStore()
    out: list[bytes] = []
    sink = EventSink(out.append, last_seq=4, buffer_max=8)
    sink.offer(3, b"old")
    replayed = await catch_up(sink, latest=lambda: 4, read=reader(store), put=await collect(out))
    assert replayed == 0
    assert out == []
    assert store.reads == []
    assert not sink.catching_up


@settings(max_examples=150, deadline=None)
@given(
    start=st.integers(0, 30),
    live=st.lists(st.integers(1, 60), max_size=40),
    buffer_max=st.integers(1, 8),
    arrival=st.integers(0, 10),
)
async def test_catch_up_never_loses_or_repeats_an_event(
    start: int, live: list[int], buffer_max: int, arrival: int
) -> None:
    store = FakeStore()
    for seq in range(1, 61):
        store.add(seq, USER if seq % 3 else OTHER)
    mine = [s for s in range(1, 61) if s % 3]
    feed = {"last": 30}
    out: list[bytes] = []
    sink = EventSink(out.append, last_seq=start, buffer_max=buffer_max)
    burst_to = max(live, default=30)  # a feed delivers every event of its user, in order
    pending = [s for s in mine if 30 < s <= burst_to]

    fired = False

    def burst() -> None:
        nonlocal fired
        fired = True
        for seq in pending:
            feed["last"] = seq
            sink.offer(seq, json.dumps({"seq": seq}).encode())

    async def put(item: bytes) -> None:
        out.append(item)
        if len(out) == arrival:  # the burst lands while replay waits for the socket
            burst()
        await asyncio.sleep(0)

    await catch_up(sink, latest=lambda: feed["last"], read=reader(store), put=put)
    if not fired:  # catch-up finished first: the burst arrives on a live sink
        burst()
    got = [json.loads(f)["seq"] for f in out]
    top = max([30, *pending])
    assert got == [s for s in mine if start < s <= top]


async def test_plan_without_a_resume_point_is_fresh() -> None:
    plan = await plan_resume(as_store(FakeStore()), USER, requested=None, current=9)
    assert (plan.mode, plan.after) == (ResumeMode.FRESH, 9)


@pytest.mark.parametrize(
    ("requested", "first", "mode"),
    [
        (4, 1, ResumeMode.REPLAY),  # the usual case
        (0, 1, ResumeMode.REPLAY),  # nothing seen yet, nothing aged out
        (9, 10, ResumeMode.REPLAY),  # the resume point aged out, but nothing after it did
        (4, 6, ResumeMode.RESET),  # events after the resume point may have aged out
        (99, 1, ResumeMode.RESET),  # ahead of the stream: from a stream that was recreated
        (5, 1, ResumeMode.RESET),  # sequence 5 belongs to somebody else
    ],
)
async def test_plan_decides_between_replay_and_reset(
    requested: int, first: int, mode: ResumeMode
) -> None:
    store = FakeStore()
    for seq in range(1, 21):
        store.add(seq, OTHER if seq == 5 else USER)
    store.first = first
    plan = await plan_resume(as_store(store), USER, requested=requested, current=20)
    assert plan.mode == mode
    assert plan.after == (requested if mode == ResumeMode.REPLAY else 20)


async def test_plan_resets_instead_of_replaying_a_huge_backlog() -> None:
    store = FakeStore()
    for seq in range(1, REPLAY_MAX_EVENTS + 3):
        store.add(seq)
    plan = await plan_resume(as_store(store), USER, requested=1, current=REPLAY_MAX_EVENTS + 2)
    assert plan.mode == ResumeMode.RESET


async def test_plan_on_an_empty_stream_replays_nothing() -> None:
    store = FakeStore()
    store.first = 0
    plan = await plan_resume(as_store(store), USER, requested=0, current=0)
    assert (plan.mode, plan.after) == (ResumeMode.REPLAY, 0)


class FakeNats:
    def __init__(self) -> None:
        self.subscribed: list[str] = []

    async def subscribe(self, subject: str, cb: Any = None) -> Any:
        self.subscribed.append(subject)
        return None

    async def flush(self, **_: object) -> None:
        return None


class Member:
    def __init__(self, last_seq: int = 0) -> None:
        self.frames: list[bytes] = []
        self.sink = EventSink(self.frames.append, last_seq=last_seq, buffer_max=64)
        self.sink.catching_up = False
        self.closed: tuple[int, str] | None = None

    def offer_text(self, frame: bytes) -> bool:
        self.frames.append(frame)
        return True

    def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)

    def seqs(self) -> list[int]:
        return [json.loads(f)["seq"] for f in self.frames if f.startswith(b'{"type":"event"')]


def live_copy(seq: int, prev: int, user: uuid.UUID = USER) -> Msg:
    return Msg(
        _client=cast("NatsClient", None),
        subject=subjects.live_events(user),
        data=b'{"n":%d}' % seq,
        headers={"Nats-Sequence": str(seq), "Nats-Last-Sequence": str(prev)},
    )


async def started_feed(store: FakeStore, *members: FeedMember) -> UserFeed:
    notices: list[bytes] = []
    feed = UserFeed(
        USER,
        nc=cast("NatsClient", FakeNats()),
        store=as_store(store),
        on_notice=lambda _, payload: notices.append(payload),
    )
    await feed.start()
    for n, member in enumerate(members):
        feed.members[str(n)] = member
    return feed


async def test_a_feed_starts_at_the_users_newest_event() -> None:
    store = FakeStore()
    store.add(4)
    store.add(6, OTHER)
    feed = await started_feed(store)
    assert feed.last_seq == 4


async def test_a_feed_delivers_in_order_and_drops_copies_it_already_has() -> None:
    store = FakeStore()
    store.add(4)
    member = Member(last_seq=4)
    feed = await started_feed(store, member)
    for seq, prev in ((9, 4), (9, 4), (3, 0), (12, 9)):
        store.add(seq)
        await feed.handle(live_copy(seq, prev))
    assert member.seqs() == [9, 12]
    assert [json.loads(f)["prev"] for f in member.frames] == [4, 9]


async def test_a_lost_live_copy_is_healed_from_the_stream_before_the_next_one() -> None:
    store = FakeStore()
    store.add(4)
    member = Member(last_seq=4)
    feed = await started_feed(store, member)
    for seq in (6, 7):  # published while this replica's copies were lost
        store.add(seq)
    store.add(10)
    await feed.handle(live_copy(10, 7))
    assert member.seqs() == [6, 7, 10]
    assert [json.loads(f)["prev"] for f in member.frames] == [4, 6, 7]
    assert store.reads[-1] == (4, 7)


async def test_healing_that_fails_still_delivers_and_shows_the_gap() -> None:
    store = FakeStore()
    member = Member()
    feed = await started_feed(store, member)
    store.fail = True
    await feed.handle(live_copy(10, 7))
    assert member.seqs() == [10]
    assert json.loads(member.frames[0])["prev"] == 7  # the client sees 7 is missing


async def test_pulses_are_forwarded_verbatim_and_notices_handed_over() -> None:
    store = FakeStore()
    member = Member()
    notices: list[tuple[uuid.UUID, bytes]] = []
    feed = UserFeed(
        USER,
        nc=cast("NatsClient", FakeNats()),
        store=as_store(store),
        on_notice=lambda user, payload: notices.append((user, payload)),
    )
    await feed.start()
    feed.members["a"] = member
    pulse = b'{"type":"pulse","window_ms":100,"zones":{}}'
    await feed.handle(
        Msg(_client=cast("NatsClient", None), subject=subjects.live_pulses(USER), data=pulse)
    )
    await feed.handle(
        Msg(_client=cast("NatsClient", None), subject=subjects.live_sessions(USER), data=b"{}")
    )
    assert member.frames == [pulse]
    assert notices == [(USER, b"{}")]


async def test_an_audit_fills_a_gap_that_no_later_event_revealed() -> None:
    store = FakeStore()
    member = Member()
    feed = await started_feed(store, member)
    store.add(3)
    store.add(5)
    await feed.audit()
    assert member.seqs() == [3, 5]
    await feed.audit()
    assert member.seqs() == [3, 5]


async def test_a_feed_that_is_still_starting_is_not_audited() -> None:
    store = FakeStore()
    store.add(3)
    member = Member()
    feed = UserFeed(
        USER,
        nc=cast("NatsClient", FakeNats()),
        store=as_store(store),
        on_notice=lambda _, __: None,
    )
    feed.members["a"] = member
    await feed.audit()
    assert member.frames == []
    assert store.reads == []


async def test_an_audit_asks_clients_to_resume_when_the_stream_was_recreated() -> None:
    store = FakeStore()
    for seq in range(1, 11):
        store.add(seq)
    member = Member(last_seq=10)
    feed = await started_feed(store, member)
    store.events = {}
    store.add(2)  # a fresh stream: sequences start again
    await feed.audit()
    assert member.closed is not None
    assert member.closed[0] == 4008
    assert feed.last_seq == 2


async def test_events_that_merely_aged_out_do_not_disturb_anyone() -> None:
    store = FakeStore()
    store.add(10)
    member = Member(last_seq=10)
    feed = await started_feed(store, member)
    store.events = {}
    store.add(40, OTHER)  # the stream moved on; this user's events expired
    store.first = 40
    await feed.audit()
    assert member.closed is None
    assert feed.last_seq == 10
