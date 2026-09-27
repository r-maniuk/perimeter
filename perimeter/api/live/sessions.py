"""Live sessions across the cluster: the registry, the per-user cap and device labels.

Every live socket is registered in the ``sessions`` bucket as ``<user>.<session>`` and refreshed
well within the bucket TTL, so a replica that dies simply lets its entries expire. Each replica
mirrors the whole bucket through one watch, which makes listing a user's sessions, enforcing the
cap and noticing expiries local operations. Joins and leaves are also announced on
``live.ses.<user>``: replicas serving that user apply the notice at once instead of waiting for
the watch, and push the fresh list to the user's sessions.

The cap is enforced without a lock. A session first registers, then waits until the mirror has
applied every write up to its own, and counts the user's sessions that were admitted before it
(by the bucket revision at which each was first seen). Two sessions racing on different replicas
see the same order, so exactly the later one is refused and the cap is never exceeded.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

import msgspec
import structlog
from nats.aio.client import Client as NatsClient
from nats.errors import Error as NatsError
from nats.js import JetStreamContext
from nats.js.errors import KeyNotFoundError
from nats.js.kv import KeyValue

from perimeter.wire import subjects

log = structlog.get_logger(__name__)

REFRESH_MAX_S = 10.0
EXPIRY_GRACE_S = 2.0
DEFAULT_TTL_S = 30.0
MIRROR_WAIT_S = 2.0
WATCH_READY_S = 10.0
REFRESH_CONCURRENCY = 32
MAX_AGENT_CHARS = 256

UNKNOWN_DEVICE = "Unknown device"

_BROWSERS: tuple[tuple[str, str], ...] = (
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("Edg/", "Edge"),
    ("Edge/", "Edge"),
    ("OPR/", "Opera"),
    ("OPiOS/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("YaBrowser/", "Yandex Browser"),
    ("Vivaldi/", "Vivaldi"),
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Chromium/", "Chromium"),
)
_CLIENTS: tuple[tuple[str, str], ...] = (
    ("websockets/", "Python websockets"),
    ("python-httpx/", "Python httpx"),
    ("aiohttp/", "Python aiohttp"),
    ("python-requests/", "Python requests"),
    ("curl/", "curl"),
    ("Wget/", "Wget"),
    ("okhttp/", "OkHttp"),
    ("Go-http-client/", "Go"),
    ("PostmanRuntime/", "Postman"),
)
_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("Windows NT", "Windows"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("CrOS", "ChromeOS"),
    ("Android", "Android"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)


def device_label(user_agent: str | None) -> str:
    """A short human label such as ``"Chrome · macOS"`` for a User-Agent header."""
    if not user_agent:
        return UNKNOWN_DEVICE
    browser = next((name for token, name in _BROWSERS if token in user_agent), None)
    if browser is None and "Safari/" in user_agent and "Version/" in user_agent:
        browser = "Safari"
    if browser is None:
        browser = next((name for token, name in _CLIENTS if token in user_agent), None)
    system = next((name for token, name in _SYSTEMS if token in user_agent), None)
    return " · ".join(part for part in (browser, system) if part) or UNKNOWN_DEVICE


class SessionRecord(msgspec.Struct, frozen=True, kw_only=True):
    """One live session as stored in the ``sessions`` bucket."""

    sid: str
    uid: str
    replica: str
    connected_at: datetime
    agent: str | None
    label: str
    ip: str | None
    jti: str

    @property
    def key(self) -> str:
        return session_key(self.uid, self.sid)


def session_key(user_id: object, sid: str) -> str:
    return f"{user_id}.{sid}"


class _Notice(msgspec.Struct, frozen=True, kw_only=True):
    op: str  # "join" | "leave"
    key: str
    revision: int = 0
    session: SessionRecord | None = None


_encoder = msgspec.json.Encoder()
_record_decoder = msgspec.json.Decoder(SessionRecord)
_notice_decoder = msgspec.json.Decoder(_Notice)


@dataclass(slots=True)
class _Entry:
    record: SessionRecord
    ticket: int  # bucket revision at which this replica first saw the session: admission order
    seen_at: float


class SessionMirror:
    """This replica's copy of the ``sessions`` bucket.

    Entries that stop being refreshed disappear after the bucket TTL, as they do in the bucket.
    Removed keys are remembered for a while, so a late write of a closed session (a refresh that
    crossed its delete) cannot bring it back.
    """

    def __init__(self, *, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._users: dict[str, set[str]] = {}
        self._removed: dict[str, float] = {}
        self.watermark = 0  # highest bucket revision applied from the watch

    def put(self, record: SessionRecord, revision: int) -> bool:
        """Apply a write; True when it adds a session."""
        key = record.key
        if key in self._removed:
            return False
        entry = self._entries.get(key)
        if entry is not None:
            entry.record = record
            entry.seen_at = self._clock()
            return False
        self._entries[key] = _Entry(record, revision, self._clock())
        self._users.setdefault(record.uid, set()).add(key)
        return True

    def remove(self, key: str) -> str | None:
        """Apply a delete; the owner's user id when a session was removed."""
        self._removed[key] = self._clock()
        return self._drop(key)

    def expire(self) -> set[str]:
        """Drop sessions nobody refreshed within the TTL; the users that lost one."""
        now = self._clock()
        cutoff = now - self._ttl_s - EXPIRY_GRACE_S
        stale = [key for key, entry in self._entries.items() if entry.seen_at < cutoff]
        users = {uid for key in stale if (uid := self._drop(key)) is not None}
        forget_before = now - 2 * self._ttl_s
        for key in [key for key, at in self._removed.items() if at < forget_before]:
            del self._removed[key]
        return users

    def get(self, key: str) -> SessionRecord | None:
        entry = self._entries.get(key)
        return entry.record if entry is not None else None

    def sessions(self, user_id: str) -> list[SessionRecord]:
        return [self._entries[key].record for key in self._users.get(user_id, ())]

    def admitted_before(self, key: str) -> int:
        """How many live sessions of the same user were admitted before ``key``."""
        entry = self._entries[key]
        cutoff = self._clock() - self._ttl_s - EXPIRY_GRACE_S
        others = (self._entries[k] for k in self._users.get(entry.record.uid, ()) if k != key)
        return sum(1 for other in others if other.ticket < entry.ticket and other.seen_at >= cutoff)

    def _drop(self, key: str) -> str | None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return None
        uid = entry.record.uid
        keys = self._users.get(uid)
        if keys is not None:
            keys.discard(key)
            if not keys:
                del self._users[uid]
        return uid


class SessionRegistry:
    def __init__(
        self,
        js: JetStreamContext,
        nc: NatsClient,
        *,
        instance: str,
        max_per_user: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._js = js
        self._nc = nc
        self._instance = instance
        self._max_per_user = max_per_user
        self._clock = clock
        self._kv: KeyValue | None = None
        self._mirror = SessionMirror(ttl_s=DEFAULT_TTL_S, clock=clock)
        self._refresh_s = REFRESH_MAX_S
        self._local: dict[str, SessionRecord] = {}
        self._listeners: list[Callable[[str], None]] = []
        self._waiters: list[tuple[int, asyncio.Future[None]]] = []
        self._watch: KeyValue.KeyWatcher | None = None
        self._follower: asyncio.Task[None] | None = None

    @property
    def instance(self) -> str:
        return self._instance

    @property
    def kv(self) -> KeyValue:
        assert self._kv is not None, "the registry has not been started"
        return self._kv

    async def start(self) -> None:
        self._kv = await self._js.key_value(subjects.KV_SESSIONS)
        ttl_s = (await self._kv.status()).ttl or DEFAULT_TTL_S
        self._mirror = SessionMirror(ttl_s=ttl_s, clock=self._clock)
        self._refresh_s = min(REFRESH_MAX_S, ttl_s / 3)
        self._watch = await self._kv.watchall()
        ready = asyncio.Event()
        self._follower = asyncio.create_task(self._follow(self._watch, ready), name="live-sessions")
        try:
            await asyncio.wait_for(ready.wait(), WATCH_READY_S)
        except TimeoutError:
            log.warning("live.sessions_initial_sync_slow")

    def on_change(self, listener: Callable[[str], None]) -> None:
        """Call ``listener(user_id)`` whenever a user's set of sessions changes."""
        self._listeners.append(listener)

    def sessions_of(self, user_id: object) -> list[SessionRecord]:
        """The user's live sessions on every replica, oldest first."""
        uid = str(user_id)
        records = {record.key: record for record in self._mirror.sessions(uid)}
        records.update((key, record) for key, record in self._local.items() if record.uid == uid)
        return sorted(records.values(), key=lambda record: (record.connected_at, record.sid))

    async def register(self, record: SessionRecord) -> bool:
        """Register a new session; False (and nothing registered) when the user is at the cap."""
        key = record.key
        self._local[key] = record
        try:
            revision = await self.kv.put(key, _encoder.encode(record))
        except BaseException:
            self._local.pop(key, None)
            raise
        if self._mirror.put(record, revision):
            self._changed(record.uid)
        try:
            await self._caught_up(revision)
        except TimeoutError:
            log.warning("live.sessions_mirror_behind", revision=revision)  # admit: soft limit
        else:
            if self._mirror.admitted_before(key) >= self._max_per_user:
                await self.unregister(record, announce=False)
                return False
        await self._announce(
            record.uid, _Notice(op="join", key=key, revision=revision, session=record)
        )
        return True

    async def unregister(self, record: SessionRecord, *, announce: bool = True) -> None:
        key = record.key
        self._local.pop(key, None)
        if self._mirror.remove(key) is not None:
            self._changed(record.uid)
        try:
            await self.kv.delete(key)
        except NatsError as exc:  # the entry expires with the bucket TTL anyway
            log.warning("live.session_delete_failed", key=key, error=repr(exc))
        if announce:
            await self._announce(record.uid, _Notice(op="leave", key=key))

    async def lookup(self, user_id: object, sid: str) -> SessionRecord | None:
        """The user's session ``sid`` as stored in the bucket (authoritative, not the mirror)."""
        key = session_key(user_id, sid)
        try:
            entry = await self.kv.get(key)
        except KeyNotFoundError:
            return None
        if not entry.value:
            return None
        try:
            record = _record_decoder.decode(entry.value)
        except msgspec.DecodeError:
            return None
        return record if record.key == key else None

    async def terminate(self, record: SessionRecord) -> None:
        """Remote sign-out: tell the owning replica to close the socket, drop the entry."""
        await self._nc.publish(
            subjects.session_control(record.sid), b'{"code":4001,"reason":"signed out"}'
        )
        await self.unregister(record)

    def apply_notice(self, user_id: uuid.UUID, payload: bytes) -> None:
        """A join or leave announced on ``live.ses.<user>`` (possibly ahead of the watch)."""
        try:
            notice = _notice_decoder.decode(payload)
        except msgspec.DecodeError:
            log.warning("live.invalid_session_notice", user=str(user_id))
            return
        uid = str(user_id)
        if not notice.key.startswith(f"{uid}."):
            return
        if notice.op == "join" and notice.session is not None and notice.session.key == notice.key:
            if self._mirror.put(notice.session, notice.revision):
                self._changed(uid)
        elif notice.op == "leave" and self._mirror.remove(notice.key) is not None:
            self._changed(uid)

    def snapshot(self) -> dict[str, int]:
        return {"sessions": len(self._local)}

    async def run(self, stop: asyncio.Event) -> None:
        """Keep this replica's entries alive and expire everybody else's that went silent."""
        loop = asyncio.get_running_loop()
        next_refresh = loop.time() + self._refresh_s
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 1.0)
            for uid in self._mirror.expire():
                self._changed(uid)
            if loop.time() >= next_refresh:
                next_refresh = loop.time() + self._refresh_s
                await self._refresh()

    async def close(self) -> None:
        if self._follower is not None:
            self._follower.cancel()
            with suppress(asyncio.CancelledError):
                await self._follower
        if self._watch is not None:
            with suppress(NatsError):
                await self._watch.stop()  # type: ignore[no-untyped-call]
        for _, waiter in self._waiters:  # the mirror stops here: nobody catches up any more
            if not waiter.done():
                waiter.set_exception(TimeoutError("the session registry is closing"))
        self._waiters.clear()

    async def _refresh(self) -> None:
        slots = asyncio.Semaphore(REFRESH_CONCURRENCY)

        async def refresh(key: str, record: SessionRecord) -> None:
            async with slots:
                try:
                    await self.kv.put(key, _encoder.encode(record))
                except NatsError as exc:
                    log.warning("live.session_refresh_failed", key=key, error=repr(exc))

        await asyncio.gather(*(refresh(k, r) for k, r in list(self._local.items())))

    async def _follow(self, watcher: KeyValue.KeyWatcher, ready: asyncio.Event) -> None:
        async for entry in watcher:
            if entry is None:  # the initial values have been delivered
                ready.set()
                continue
            try:
                self._apply(entry)
            except Exception:  # one bad entry must not stop the mirror
                log.exception("live.session_entry_failed", key=entry.key)

    def _apply(self, entry: KeyValue.Entry) -> None:
        revision = entry.revision or 0
        if entry.operation is None and entry.value:
            try:
                record = _record_decoder.decode(entry.value)
            except msgspec.DecodeError:
                log.warning("live.invalid_session_entry", key=entry.key)
            else:
                if record.key == entry.key and self._mirror.put(record, revision):
                    self._changed(record.uid)
        elif entry.operation is not None and (uid := self._mirror.remove(entry.key)):
            self._changed(uid)
        self._mirror.watermark = max(self._mirror.watermark, revision)
        self._release_waiters()

    async def _caught_up(self, revision: int) -> None:
        if self._mirror.watermark >= revision:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((revision, waiter))
        try:
            await asyncio.wait_for(asyncio.shield(waiter), MIRROR_WAIT_S)
        finally:
            if (revision, waiter) in self._waiters:
                self._waiters.remove((revision, waiter))

    def _release_waiters(self) -> None:
        if not self._waiters:
            return
        watermark = self._mirror.watermark
        remaining = []
        for revision, waiter in self._waiters:
            if revision <= watermark:
                if not waiter.done():
                    waiter.set_result(None)
            else:
                remaining.append((revision, waiter))
        self._waiters = remaining

    def _changed(self, user_id: str) -> None:
        for listener in self._listeners:
            listener(user_id)

    async def _announce(self, user_id: str, notice: _Notice) -> None:
        try:
            await self._nc.publish(subjects.live_sessions(user_id), _encoder.encode(notice))
        except NatsError as exc:
            log.warning("live.session_notice_failed", user=user_id, error=repr(exc))
