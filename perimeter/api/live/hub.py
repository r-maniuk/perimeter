"""The live hub: every live socket of this replica and the broker subscriptions that feed them.

:meth:`LiveHub.serve` runs one socket from accept to close:

1. the resume point: ``?resume_after=`` or a first ``resume`` message within a short window;
2. admission in the cluster-wide session registry (over the per-user cap: close 4009);
3. the user's feed (one ``live.*.<user>`` subscription per user and replica);
4. the resume plan, then ``hello`` (always the first frame) and the reader and writer tasks;
5. catch-up: events are replayed from the stream while live ones buffer, then live delivery;
6. teardown on every exit path: interest, snapshots, socket, feed, registry.

Positions reach sessions through the interest trie (one subscription per cover prefix), events and
pulses through user feeds, remote sign-outs through ``ctl.ses.*`` and the revocation list.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial

import msgspec
import structlog
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from nats.errors import Error as NatsError
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.websockets import WebSocket

from perimeter.api.live import metrics
from perimeter.api.live.connection import ConnectionClosing, LiveConnection
from perimeter.api.live.events import (
    EventSink,
    EventStore,
    EventStoreError,
    ResumePlan,
    UserFeed,
    UserFeeds,
    catch_up,
    plan_resume,
    replay_frames,
)
from perimeter.api.live.interest import InterestTrie, TileSubscriptions
from perimeter.api.live.ops import OpsBoard
from perimeter.api.live.protocol import (
    PROTOCOL_VERSION,
    ClientMessage,
    CloseCode,
    Hello,
    OpsToggle,
    Ping,
    Pong,
    ProtocolError,
    Resume,
    ResumeView,
    Sessions,
    SessionView,
    UserView,
    Viewport,
    decode_client,
    encode,
    now_ms,
)
from perimeter.api.live.sessions import (
    MAX_AGENT_CHARS,
    SessionRecord,
    SessionRegistry,
    device_label,
)
from perimeter.api.live.snapshot import SnapshotService
from perimeter.api.security import Principal, RevocationList
from perimeter.config import Settings
from perimeter.domain import tiles
from perimeter.wire import subjects

log = structlog.get_logger(__name__)

RESUME_WINDOW_S = 0.5
SESSIONS_PUSH_DELAY_S = 0.05
MAINTENANCE_INTERVAL_S = 1.0
AUDIT_INTERVAL_S = 10.0
CLOSE_GRACE_S = 5.0
SNAPSHOT_POOL_SHARE = 4  # snapshot queries may hold at most a quarter of the database pool

BROKER_ERRORS: tuple[type[Exception], ...] = (NatsError, EventStoreError, TimeoutError, OSError)


class _Control(msgspec.Struct, frozen=True):
    code: int = CloseCode.SIGNED_OUT
    reason: str = "signed out"


_control_decoder = msgspec.json.Decoder(_Control)


class _SnapshotFiller:
    """Sends snapshots of newly covered prefixes, one batch at a time, at the socket's pace."""

    def __init__(
        self,
        service: SnapshotService,
        conn: LiveConnection,
        held: Callable[[], frozenset[str]],
    ) -> None:
        self._service = service
        self._conn = conn
        self._held = held
        self._pending: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def request(self, prefixes: Iterable[str]) -> None:
        self._pending.update(prefixes)
        if self._pending and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._fill(), name="live-snapshots")

    async def _fill(self) -> None:
        with suppress(ConnectionClosing):
            while self._pending:
                wanted = sorted(self._pending & self._held())
                self._pending.clear()
                frames = await asyncio.gather(
                    *(self._service.frame(prefix) for prefix in wanted), return_exceptions=True
                )
                for prefix, frame in zip(wanted, frames, strict=True):
                    if isinstance(frame, bytes) and prefix in self._held():
                        await self._conn.put_position(frame)

    async def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task


class LiveSession:
    """One socket of one user: connection, position in the event chain, snapshot work."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        principal: Principal,
        record: SessionRecord,
        conn: LiveConnection,
        snapshots: SnapshotService,
        held: Callable[[], frozenset[str]],
        event_buffer: int,
    ) -> None:
        self.websocket = websocket
        self.principal = principal
        self.record = record
        self.conn = conn
        self.sink = EventSink(conn.offer_text, buffer_max=event_buffer)
        self.snapshots = _SnapshotFiller(snapshots, conn, held)
        self.feed: UserFeed | None = None
        self.registered = False
        self.sessions_frame: bytes | None = None
        self.done = asyncio.Event()

    @property
    def sid(self) -> str:
        return self.record.sid

    def offer_text(self, frame: bytes) -> bool:
        return self.conn.offer_text(frame)

    def close(self, code: int, reason: str = "") -> None:
        self.conn.close(code, reason)


class LiveHub:
    def __init__(
        self,
        settings: Settings,
        *,
        nc: NatsClient,
        js: JetStreamContext,
        db: AsyncEngine,
        registry: SessionRegistry,
        ops: OpsBoard,
        revoked: RevocationList,
        instance: str,
    ) -> None:
        self._live = settings.live
        self._nc = nc
        self._registry = registry
        self._ops = ops
        self._revoked = revoked
        self._instance = instance
        self._counters = metrics.Counters()
        self._rates = metrics.Rates(self._counters)
        self._sessions: dict[str, LiveSession] = {}
        self._trie: InterestTrie[str] = InterestTrie()
        self._tiles = TileSubscriptions(
            nc, self._trie, leaf_zoom=self._live.tile_zoom, on_frame=self._on_position
        )
        self._store = EventStore(nc, js)
        self._feeds = UserFeeds(nc, self._store, on_notice=registry.apply_notice)
        self._snapshots = SnapshotService(
            db,
            stale_s=self._live.device_stale_s,
            cache_s=self._live.snapshot_cache_s,
            concurrency=settings.database.pool_size // SNAPSHOT_POOL_SHARE,
        )
        self._control: Subscription | None = None
        self._revocations: asyncio.Queue[str] | None = None
        self._pushes: dict[str, asyncio.TimerHandle] = {}
        self._closing = False
        registry.on_change(self._sessions_changed)

    @property
    def feeds(self) -> UserFeeds:
        return self._feeds

    @property
    def tiles(self) -> TileSubscriptions[str]:
        return self._tiles

    def session(self, sid: str) -> LiveSession | None:
        return self._sessions.get(sid)

    async def start(self) -> None:
        self._control = await self._nc.subscribe(subjects.session_control("*"), cb=self._on_control)
        self._revocations = self._revoked.subscribe()

    def snapshot(self) -> dict[str, float]:
        return self._rates.read()

    async def run(self, stop: asyncio.Event) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._follow_revocations(), name="live-revocations")
            group.create_task(self._maintain(stop), name="live-maintenance")

    async def close(self) -> None:
        """Close every socket with 1001 and drop the subscriptions, before NATS drains."""
        self._closing = True
        for handle in self._pushes.values():
            handle.cancel()
        self._pushes.clear()
        sessions = list(self._sessions.values())
        for session in sessions:
            session.close(CloseCode.GOING_AWAY, "server shutting down")
        if sessions:
            with suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_GRACE_S):
                    await asyncio.gather(*(session.done.wait() for session in sessions))
        control, self._control = self._control, None
        if control is not None:
            with suppress(NatsError):
                await control.unsubscribe()
        await self._tiles.close()
        await self._feeds.close()
        await self._snapshots.close()

    # --- one socket --------------------------------------------------------------------------

    async def serve(self, websocket: WebSocket, principal: Principal) -> None:
        """Run an accepted, authenticated socket until it closes."""
        session = self._new_session(websocket, principal)
        if self._closing:
            session.close(CloseCode.GOING_AWAY, "server shutting down")
            await session.conn.shutdown()
            return
        self._sessions[session.sid] = session
        try:
            await self._run(session)
        except Exception:
            log.exception("live.session_failed", sid=session.sid)
            session.close(CloseCode.INTERNAL_ERROR, "internal error")
        finally:
            await self._finish(session)

    def _new_session(self, websocket: WebSocket, principal: Principal) -> LiveSession:
        agent = websocket.headers.get("user-agent")
        record = SessionRecord(
            sid=str(uuid.uuid7()),
            uid=str(principal.user_id),
            replica=self._instance,
            connected_at=datetime.now(UTC),
            agent=agent[:MAX_AGENT_CHARS] if agent else None,
            label=device_label(agent),
            ip=websocket.client.host if websocket.client else None,
            jti=principal.token_id,
        )
        conn = LiveConnection(
            websocket,
            send_timeout_s=self._live.send_timeout_s,
            text_capacity=self._live.event_queue_max,
            position_budget=self._live.position_budget_bytes,
            counters=self._counters,
        )
        return LiveSession(
            websocket=websocket,
            principal=principal,
            record=record,
            conn=conn,
            snapshots=self._snapshots,
            held=partial(self._trie.prefixes, record.sid),
            event_buffer=self._live.event_queue_max,
        )

    async def _run(self, session: LiveSession) -> None:
        requested, pending = await self._opening(session)
        if session.conn.closing:
            return
        if self._revoked.is_revoked(session.principal.token_id):
            session.close(CloseCode.SIGNED_OUT, "signed out")
            return
        user_id = session.principal.user_id
        try:
            if not await self._registry.register(session.record):
                session.close(CloseCode.TOO_MANY_SESSIONS, "too many live sessions for this user")
                return
            session.registered = True
            metrics.SESSIONS.inc()
            session.feed = feed = await self._feeds.attach(user_id, session.sid, session)
            plan = await plan_resume(
                self._store, user_id, requested=requested, current=feed.last_seq
            )
        except BROKER_ERRORS as exc:
            log.warning("live.broker_unavailable", sid=session.sid, error=repr(exc))
            session.close(CloseCode.TRY_AGAIN_LATER, "event stream unavailable, retry shortly")
            return
        session.sink.last_seq = plan.after
        metrics.RESUMES.labels(plan.mode.value).inc()
        session.conn.start(self._hello(session, plan), partial(self._on_message, session), pending)
        self._send_sessions(session, self._registry.sessions_of(user_id))
        try:
            replayed = await catch_up(
                session.sink,
                latest=lambda: feed.last_seq,
                read=partial(replay_frames, self._store, user_id),
                put=session.conn.put_text,
            )
        except ConnectionClosing:
            return
        except BROKER_ERRORS as exc:
            log.warning("live.replay_failed", sid=session.sid, error=repr(exc))
            session.close(CloseCode.TRY_AGAIN_LATER, "event stream unavailable, retry shortly")
            return
        log.info(
            "live.session_started",
            sid=session.sid,
            user=str(user_id),
            resume=plan.mode.value,
            after=plan.after,
            replayed=replayed,
        )
        await self._until_closed_or_expired(session)

    @staticmethod
    async def _until_closed_or_expired(session: LiveSession) -> None:
        """Serve until the socket closes; a socket must not outlive the token that opened it."""
        left_s = session.principal.expires_at - now_ms() / 1000
        try:
            async with asyncio.timeout(max(0.0, left_s)):
                await session.conn.wait_closing()
        except TimeoutError:
            session.close(CloseCode.SIGNED_OUT, "session expired: sign in again")

    async def _opening(self, session: LiveSession) -> tuple[int | None, ClientMessage | None]:
        """The resume point, and a first message that was not a resume (handled later)."""
        query = session.websocket.query_params.get("resume_after")
        if query is not None:
            if query.isascii() and query.isdigit() and len(query) <= 19:
                return int(query), None
            session.close(CloseCode.PROTOCOL_ERROR, "resume_after must be a sequence number")
            return None, None
        first = await self._first_message(session)
        if isinstance(first, Resume):
            return first.after, None
        return None, first

    @staticmethod
    async def _first_message(session: LiveSession) -> ClientMessage | None:
        """The client's first message, if it arrives within the resume window."""
        try:
            async with asyncio.timeout(RESUME_WINDOW_S):
                message = await session.websocket.receive()
        except TimeoutError:
            return None
        if message["type"] == "websocket.disconnect":
            session.conn.mark_peer_closed(message.get("code"))
            return None
        text = message.get("text")
        if text is None:
            session.close(CloseCode.PROTOCOL_ERROR, "binary messages are not part of the protocol")
            return None
        try:
            return decode_client(text)
        except ProtocolError as exc:
            session.close(CloseCode.PROTOCOL_ERROR, str(exc))
            return None

    def _hello(self, session: LiveSession, plan: ResumePlan) -> bytes:
        return encode(
            Hello(
                session_id=session.sid,
                user=UserView(
                    id=str(session.principal.user_id), username=session.principal.username
                ),
                server_time=now_ms(),
                protocol=PROTOCOL_VERSION,
                resume=ResumeView(mode=plan.mode, after=plan.after),
                tile_zoom=self._live.tile_zoom,
                replica=self._instance,
            )
        )

    async def _finish(self, session: LiveSession) -> None:
        self._sessions.pop(session.sid, None)
        self._ops.unwatch(session)
        change = self._trie.remove(session.sid)
        if change.subscribe or change.unsubscribe:
            self._tiles.sync()
        await session.snapshots.cancel()
        await session.conn.shutdown()
        if session.feed is not None:
            await self._quietly(
                self._feeds.detach(session.principal.user_id, session.sid), "feed_detach"
            )
        if session.registered:
            metrics.SESSIONS.dec()
            await self._quietly(self._registry.unregister(session.record), "unregister")
        by_peer = session.conn.closed_by_peer
        code = "peer" if by_peer else str(session.conn.close_code)
        metrics.CLOSED.labels(code).inc()
        session.done.set()
        log.info(
            "live.session_ended",
            sid=session.sid,
            code=code,
            peer_code=session.conn.peer_close_code if by_peer else None,
        )

    @staticmethod
    async def _quietly(work: Awaitable[None], step: str) -> None:
        try:
            await work
        except Exception:  # teardown goes on: every remaining step still has to run
            log.warning("live.teardown_step_failed", step=step, exc_info=True)

    # --- client messages ---------------------------------------------------------------------

    async def _on_message(self, session: LiveSession, message: ClientMessage) -> None:
        if self._sessions.get(session.sid) is not session:
            # Torn down already (its reader may still hand over a message while the teardown
            # awaits): nothing a closed session asks for may be set up again, or it would leak.
            return
        match message:
            case Viewport():
                self._set_viewport(session, message)
            case Ping(t=t):
                session.conn.offer_text(encode(Pong(t=t, server_time=now_ms())))
            case OpsToggle(on=True):
                self._ops.watch(session)
            case OpsToggle(on=False):
                self._ops.unwatch(session)
            case Resume():
                pass  # the resume point is taken when the session starts

    def _set_viewport(self, session: LiveSession, viewport: Viewport) -> None:
        prefixes = tiles.covering_quadkeys(
            tiles.BBox(*viewport.bbox),
            max_tiles=self._live.max_viewport_tiles,
            max_zoom=self._live.tile_zoom,
        )
        change = self._trie.update(session.sid, prefixes)
        if change.subscribe or change.unsubscribe:
            self._tiles.sync()
        everything = session.conn.resume_positions()  # after a resync the client needs it all
        session.snapshots.request(prefixes if everything else change.added)

    # --- broker callbacks --------------------------------------------------------------------

    def _on_position(self, subject: str, frame: bytes) -> None:
        try:
            key = tiles.quadkey_of_subject(subject)
        except ValueError:
            return
        for sid in self._trie.targets(key):
            session = self._sessions.get(sid)
            if session is not None:
                session.conn.offer_position(frame)

    async def _on_control(self, msg: Msg) -> None:
        session = self._sessions.get(msg.subject.rsplit(".", 1)[-1])
        if session is None:
            return
        try:
            control = _control_decoder.decode(msg.data) if msg.data else _Control()
        except msgspec.DecodeError:
            control = _Control()
        code = control.code if 4000 <= control.code <= 4999 else CloseCode.SIGNED_OUT
        session.close(code, control.reason)

    async def _follow_revocations(self) -> None:
        assert self._revocations is not None, "start() subscribes to revocations"
        while True:
            token_id = await self._revocations.get()
            for session in list(self._sessions.values()):
                if session.principal.token_id == token_id:
                    session.close(CloseCode.SIGNED_OUT, "signed out")

    def _close_revoked(self) -> None:
        """The sweep behind the revocation queue: no signed-out socket stays open for long."""
        for session in list(self._sessions.values()):
            if not session.conn.closing and self._revoked.is_revoked(session.principal.token_id):
                session.close(CloseCode.SIGNED_OUT, "signed out")

    async def _maintain(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        reconnects = self._nc.stats["reconnects"]
        next_audit = loop.time() + AUDIT_INTERVAL_S
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), MAINTENANCE_INTERVAL_S)
            self._snapshots.prune()
            self._close_revoked()
            current = self._nc.stats["reconnects"]
            if current == reconnects and loop.time() < next_audit:
                continue
            reconnects, next_audit = current, loop.time() + AUDIT_INTERVAL_S
            try:
                await self._feeds.audit()
            except Exception:
                log.exception("live.audit_failed")

    # --- session lists -----------------------------------------------------------------------

    def _sessions_changed(self, user_id: str) -> None:
        if user_id in self._pushes or self._closing:
            return
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return
        if self._feeds.get(uid) is None:
            return
        loop = asyncio.get_running_loop()
        self._pushes[user_id] = loop.call_later(SESSIONS_PUSH_DELAY_S, self._push_sessions, uid)

    def _push_sessions(self, user_id: uuid.UUID) -> None:
        self._pushes.pop(str(user_id), None)
        feed = self._feeds.get(user_id)
        if feed is None:
            return
        records = self._registry.sessions_of(user_id)
        for sid in list(feed.members):
            session = self._sessions.get(sid)
            if session is not None:
                self._send_sessions(session, records)

    @staticmethod
    def _send_sessions(session: LiveSession, records: list[SessionRecord]) -> None:
        """The user's sessions; ``current`` marks those of the recipient's own sign-in (as on
        ``GET /v1/sessions``: signing one of them out signs the recipient out too)."""
        own_sign_in = session.principal.token_id
        frame = encode(
            Sessions(
                sessions=[
                    SessionView(
                        sid=record.sid,
                        label=record.label,
                        agent=record.agent,
                        ip=record.ip,
                        replica=record.replica,
                        connected_at=record.connected_at,
                        current=record.jti == own_sign_in,
                    )
                    for record in records
                ]
            )
        )
        if frame != session.sessions_frame:
            session.sessions_frame = frame
            session.conn.offer_text(frame)
