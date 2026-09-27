"""User events on the live channel: per-user sequencing, gap healing and resume.

Each user has one subject in the EVENTS stream (``evt.<user>``), so the stream sequences of that
subject form the user's event chain. JetStream republishes every stored event on
``live.evt.<user>`` with ``Nats-Sequence`` (its sequence) and ``Nats-Last-Sequence`` (the previous
sequence of the same subject); every event frame carries both as ``seq`` and ``prev``.

A replica subscribes to ``live.*.<user>`` while it serves at least one session of that user (one
subscription carries the user's events, occupancy pulses and session notices) and remembers the
last sequence it delivered. A live copy whose ``prev`` differs from it means a core message was
lost (core NATS is at-most-once), so the gap is read back from the stream before anything else
is delivered. Sessions deduplicate by sequence, which makes the overlaps between healing, replay
and live delivery harmless.

Reads use JetStream *direct get* with ``next_by_subj`` and ``batch``: a stateless request served
by the stream (``allow_direct``) that returns the user's next events in order. Unlike an ephemeral
consumer it leaves nothing behind on the server, so a reconnect storm does not create a consumer
per client, and it is pull-based: a replay advances only as fast as its client takes frames.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

import msgspec
import structlog
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from nats.errors import Error as NatsError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext
from nats.js.errors import NotFoundError

from perimeter.api.live import metrics
from perimeter.api.live.protocol import CloseCode, ResumeMode
from perimeter.ops import tracing
from perimeter.wire import subjects
from perimeter.wire.events import live_frame

log = structlog.get_logger(__name__)

READ_BATCH = 256
READ_TIMEOUT_S = 2.0
FLUSH_TIMEOUT_S = 2
HEAL_TIMEOUT_S = 3.0
REPLAY_MAX_EVENTS = 5_000  # beyond this, reloading state over REST is cheaper for everybody
AUDIT_CONCURRENCY = 16

_EVENT = "evt"  # second token of live.evt.<user>, live.occ.<user>, live.ses.<user>
_PULSE = "occ"
_NOTICE = "ses"

_request_encoder = msgspec.json.Encoder()


def feed_subject(user_id: uuid.UUID) -> str:
    """Everything live about one user: ``live.evt``, ``live.occ`` and ``live.ses``."""
    return subjects.live_of_user(user_id)


class EventStoreError(RuntimeError):
    """The EVENTS stream could not be read."""


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    payload: bytes


class EventStore:
    """Reads a user's events straight from the EVENTS stream."""

    def __init__(
        self,
        nc: NatsClient,
        js: JetStreamContext,
        *,
        stream: str = subjects.EVENTS_STREAM,
        batch: int = READ_BATCH,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self._nc = nc
        self._js = js
        self._stream = stream
        self._batch = batch
        self._read_timeout_s = read_timeout_s

    async def last_seq(self, user_id: uuid.UUID) -> int:
        """Sequence of the user's newest stored event, 0 when there is none."""
        try:
            message = await self._js.get_last_msg(
                self._stream, subjects.events(user_id), direct=True
            )
        except NotFoundError:
            return 0
        return message.seq or 0

    async def bounds(self) -> tuple[int, int]:
        """First and last sequence the stream still holds."""
        info = await self._js.stream_info(self._stream)
        return info.state.first_seq, info.state.last_seq

    async def subject_at(self, seq: int) -> str | None:
        try:
            message = await self._js.get_msg(self._stream, seq=seq, direct=True)
        except NotFoundError:
            return None
        return message.subject

    async def count_after(self, user_id: uuid.UUID, after: int) -> int:
        """How many of the user's events follow sequence ``after``."""
        events, pending = await self._read_batch(subjects.events(user_id), after + 1, 1)
        return len(events) + pending if events else 0

    async def read(
        self, user_id: uuid.UUID, *, after: int, until: int | None = None
    ) -> AsyncIterator[StoredEvent]:
        """The user's events with ``after < seq <= until`` (or up to the newest), in order."""
        subject = subjects.events(user_id)
        start = after + 1
        while until is None or start <= until:
            count = self._batch if until is None else min(self._batch, until - start + 1)
            events, pending = await self._read_batch(subject, start, count)
            for event in events:
                if until is not None and event.seq > until:
                    return
                yield event
            if not events or pending == 0:
                return
            start = events[-1].seq + 1

    async def _read_batch(
        self, subject: str, start: int, count: int
    ) -> tuple[list[StoredEvent], int]:
        """One direct-get batch: events from ``start`` and how many more follow the batch."""
        inbox = self._nc.new_inbox()
        subscription = await self._nc.subscribe(inbox)
        try:
            request = {"seq": start, "next_by_subj": subject, "batch": count}
            await self._nc.publish(
                f"$JS.API.DIRECT.GET.{self._stream}",
                _request_encoder.encode(request),
                reply=inbox,
            )
            return await self._collect(subscription)
        finally:
            with suppress(NatsError):
                await subscription.unsubscribe()

    async def _collect(self, subscription: Subscription) -> tuple[list[StoredEvent], int]:
        events: list[StoredEvent] = []
        pending = 0
        while True:
            try:
                message = await subscription.next_msg(timeout=self._read_timeout_s)
            except NatsTimeoutError as exc:
                msg = "the EVENTS stream did not answer a direct get in time"
                raise EventStoreError(msg) from exc
            headers = message.headers or {}
            status = headers.get("Status")
            if status is not None and not message.data:
                if status == "404":  # nothing (more) stored on that subject
                    return events, 0
                if status == "204":  # end of batch
                    return events, int(headers.get("Nats-Num-Pending", "0"))
                msg = f"direct get from the EVENTS stream failed with status {status}"
                raise EventStoreError(msg)
            events.append(StoredEvent(int(headers["Nats-Sequence"]), message.data))
            pending = int(headers.get("Nats-Num-Pending", pending))


class EventSink:
    """One session's position in its user's event chain.

    Frames at or below ``last_seq`` are dropped (the client has them). While the session is
    catching up, newer frames are buffered instead of delivered; if the buffer overflows it is
    discarded and ``overflowed`` tells the catch-up to read those events from the stream instead.
    """

    def __init__(
        self, deliver: Callable[[bytes], object], *, last_seq: int = 0, buffer_max: int
    ) -> None:
        self.last_seq = last_seq
        self.catching_up = True
        self.overflowed = False
        self._deliver = deliver
        self._buffer: deque[tuple[int, bytes]] = deque()
        self._buffer_max = buffer_max

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def offer(self, seq: int, frame: bytes) -> None:
        if seq <= self.last_seq:
            return
        if not self.catching_up:
            self.last_seq = seq
            self._deliver(frame)
            return
        if self.overflowed:
            return
        if len(self._buffer) >= self._buffer_max:
            self._buffer.clear()
            self.overflowed = True
            return
        self._buffer.append((seq, frame))

    def take_buffered(self) -> tuple[int, bytes] | None:
        return self._buffer.popleft() if self._buffer else None

    def restart_buffer(self) -> None:
        self._buffer.clear()
        self.overflowed = False


type ReadFrames = Callable[[int, int], AsyncIterator[tuple[int, bytes]]]


async def catch_up(
    sink: EventSink,
    *,
    latest: Callable[[], int],
    read: ReadFrames,
    put: Callable[[bytes], Awaitable[None]],
) -> int:
    """Bring ``sink`` from its ``last_seq`` to live delivery without gaps or duplicates.

    Events up to the feed's current position are read from the stream while live frames collect
    in the sink's buffer; then the buffer is flushed and the sink switches to live delivery. If
    the buffer overflowed meanwhile, the missing frames are simply read from the stream in
    another round. Returns how many frames came from the stream.
    """
    replayed = 0
    while True:
        if sink.overflowed:
            sink.restart_buffer()  # before reading the target, so no frame falls in between
        target = latest()
        if sink.last_seq < target:
            async for seq, frame in read(sink.last_seq, target):
                if seq > sink.last_seq:
                    await put(frame)
                    sink.last_seq = seq
                    replayed += 1
        while not sink.overflowed and (item := sink.take_buffered()) is not None:
            seq, frame = item
            if seq > sink.last_seq:
                await put(frame)
                sink.last_seq = seq
        if not sink.overflowed:
            sink.catching_up = False  # no await since the buffer was found empty: nothing slips
            return replayed


@dataclass(frozen=True, slots=True)
class ResumePlan:
    mode: ResumeMode
    after: int


async def plan_resume(
    store: EventStore, user_id: uuid.UUID, *, requested: int | None, current: int
) -> ResumePlan:
    """Decide how a session starts: replay after ``requested``, or start from ``current``.

    A resume point is usable when nothing after it has aged out of the stream, when it is one of
    this user's events (a sequence from a recreated stream is not) and when the replay would not
    be larger than reloading state over REST.
    """
    if requested is None:
        return ResumePlan(ResumeMode.FRESH, current)
    first, last = await store.bounds()
    if requested > last or requested + 1 < first:
        return ResumePlan(ResumeMode.RESET, current)
    retained = requested >= max(first, 1)  # then it must be one of this user's events
    if retained and await store.subject_at(requested) != subjects.events(user_id):
        return ResumePlan(ResumeMode.RESET, current)
    if await store.count_after(user_id, requested) > REPLAY_MAX_EVENTS:
        return ResumePlan(ResumeMode.RESET, current)
    return ResumePlan(ResumeMode.REPLAY, requested)


async def replay_frames(
    store: EventStore, user_id: uuid.UUID, after: int, until: int
) -> AsyncIterator[tuple[int, bytes]]:
    """Event frames read from the stream, chained with ``prev`` like live ones."""
    prev = after
    async for event in store.read(user_id, after=after, until=until):
        metrics.EVENTS.labels("replayed").inc()
        yield event.seq, live_frame(event.seq, prev, event.payload)
        prev = event.seq


class FeedMember(Protocol):
    """A session as seen by its user's feed."""

    @property
    def sink(self) -> EventSink: ...

    def offer_text(self, frame: bytes) -> bool: ...

    def close(self, code: int, reason: str = "") -> None: ...


class UserFeed:
    """Live delivery of one user's events, pulses and session notices on this replica."""

    def __init__(
        self,
        user_id: uuid.UUID,
        *,
        nc: NatsClient,
        store: EventStore,
        on_notice: Callable[[uuid.UUID, bytes], None],
    ) -> None:
        self.user_id = user_id
        self.last_seq = 0
        self.members: dict[str, FeedMember] = {}
        self._nc = nc
        self._store = store
        self._on_notice = on_notice
        self._lock = asyncio.Lock()  # one event (or gap) at a time, in sequence order
        self._ready = asyncio.Event()
        self._subscription: Subscription | None = None
        self.started: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Subscribe first, then read the newest stored sequence: nothing falls in between."""
        await self.subscribe()
        await self._nc.flush(timeout=FLUSH_TIMEOUT_S)
        self.last_seq = await self._store.last_seq(self.user_id)
        self._ready.set()

    async def subscribe(self) -> None:
        self._subscription = await self._nc.subscribe(feed_subject(self.user_id), cb=self.handle)

    @property
    def subscription(self) -> Subscription | None:
        return self._subscription

    async def stop(self) -> None:
        if self.started is not None and not self.started.done():
            self.started.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.started
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            with suppress(NatsError):
                await subscription.unsubscribe()

    async def handle(self, msg: Msg) -> None:
        """One message on ``live.*.<user>`` (the subscription callback)."""
        await self._ready.wait()
        kind = msg.subject.split(".", 2)[1]
        if kind == _EVENT:
            await self._on_event(msg)
        elif kind == _PULSE:
            for member in list(self.members.values()):
                member.offer_text(msg.data)
        elif kind == _NOTICE:
            self._on_notice(self.user_id, msg.data)

    async def _on_event(self, msg: Msg) -> None:
        headers = msg.headers or {}
        try:
            seq = int(headers["Nats-Sequence"])
            prev = int(headers["Nats-Last-Sequence"])
        except KeyError, ValueError:
            log.warning("live.event_without_sequence", subject=msg.subject)
            return
        async with self._lock:
            if seq <= self.last_seq:
                return
            # The event carries the trace of the transaction that raised it (the relay adds it), so
            # one trace runs from the device's report to the sockets that show the alert.
            with tracing.span("live.deliver", headers=headers, seq=seq, sessions=len(self.members)):
                if prev > self.last_seq:
                    await self._heal(until=prev)
                self._deliver(seq, prev, msg.data, path="live")

    async def audit(self) -> None:
        """Compare with the stream: fill gaps nothing followed, detect a recreated stream."""
        if not self._ready.is_set():  # still starting: its position is being read right now
            return
        latest = await self._store.last_seq(self.user_id)
        async with self._lock:
            if latest > self.last_seq:
                await self._heal(until=latest)
            elif latest < self.last_seq:
                # Also true when the user's events merely aged out; only a stream whose
                # sequence went backwards was recreated, and then every client must resume.
                _, stream_last = await self._store.bounds()
                if stream_last < self.last_seq:
                    log.warning("live.event_stream_recreated", user=str(self.user_id))
                    self.last_seq = latest
                    for member in list(self.members.values()):
                        member.close(CloseCode.EVENTS_OVERFLOW, "event stream restarted: resume")

    async def _heal(self, *, until: int) -> None:
        healed = 0
        try:
            async with asyncio.timeout(HEAL_TIMEOUT_S):
                async for event in self._store.read(self.user_id, after=self.last_seq, until=until):
                    self._deliver(event.seq, self.last_seq, event.payload, path="healed")
                    healed += 1
        except (TimeoutError, EventStoreError, NatsError) as exc:
            # Deliver on regardless: the frame's ``prev`` shows the client the gap, and a
            # reconnect with resume_after fills it once the stream is reachable again.
            metrics.HEAL_FAILURES.inc()
            log.warning("live.heal_failed", user=str(self.user_id), error=repr(exc))
        if healed:
            log.info("live.gap_healed", user=str(self.user_id), events=healed)

    def _deliver(self, seq: int, prev: int, payload: bytes, *, path: str) -> None:
        frame = live_frame(seq, prev, payload)
        self.last_seq = seq
        metrics.EVENTS.labels(path).inc()
        for member in list(self.members.values()):
            member.sink.offer(seq, frame)


class UserFeeds:
    """The feeds of every user with a session on this replica (one subscription per user)."""

    def __init__(
        self,
        nc: NatsClient,
        store: EventStore,
        *,
        on_notice: Callable[[uuid.UUID, bytes], None],
    ) -> None:
        self._nc = nc
        self._store = store
        self._on_notice = on_notice
        self._feeds: dict[uuid.UUID, UserFeed] = {}

    def __len__(self) -> int:
        return len(self._feeds)

    def get(self, user_id: uuid.UUID) -> UserFeed | None:
        return self._feeds.get(user_id)

    async def attach(self, user_id: uuid.UUID, sid: str, member: FeedMember) -> UserFeed:
        """Add a session to its user's feed, starting the feed for the user's first session."""
        feed = self._feeds.get(user_id)
        if feed is None:
            feed = UserFeed(user_id, nc=self._nc, store=self._store, on_notice=self._on_notice)
            self._feeds[user_id] = feed
            feed.started = asyncio.create_task(feed.start(), name="live-feed-start")
            metrics.USER_FEEDS.set(len(self._feeds))
        feed.members[sid] = member
        assert feed.started is not None
        try:
            await asyncio.shield(feed.started)
        except BaseException:
            feed.members.pop(sid, None)
            if self._feeds.get(user_id) is feed and (not feed.members or feed.started.done()):
                del self._feeds[user_id]
                metrics.USER_FEEDS.set(len(self._feeds))
                await feed.stop()
            raise
        return feed

    async def detach(self, user_id: uuid.UUID, sid: str) -> None:
        feed = self._feeds.get(user_id)
        if feed is None or feed.members.pop(sid, None) is None:
            return
        if not feed.members:
            del self._feeds[user_id]
            metrics.USER_FEEDS.set(len(self._feeds))
            await feed.stop()

    async def audit(self) -> None:
        """Audit every feed against the stream (after reconnects and periodically)."""
        slots = asyncio.Semaphore(AUDIT_CONCURRENCY)

        async def audit_one(feed: UserFeed) -> None:
            async with slots:
                try:
                    await feed.audit()
                except (TimeoutError, EventStoreError, NatsError) as exc:
                    log.warning("live.audit_failed", user=str(feed.user_id), error=repr(exc))

        await asyncio.gather(*(audit_one(feed) for feed in list(self._feeds.values())))

    async def close(self) -> None:
        feeds = list(self._feeds.values())
        self._feeds.clear()
        metrics.USER_FEEDS.set(0)
        await asyncio.gather(*(feed.stop() for feed in feeds), return_exceptions=True)
