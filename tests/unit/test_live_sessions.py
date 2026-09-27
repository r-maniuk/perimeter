from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import msgspec
import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext

from perimeter.api.live import sessions as sessions_module
from perimeter.api.live.sessions import (
    SessionMirror,
    SessionRecord,
    SessionRegistry,
    device_label,
)

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"
)
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/26.0 Safari/605.1.15"
)
FIREFOX_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0"
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Mobile Safari/537.36"
)
SAMSUNG = (
    "Mozilla/5.0 (Linux; Android 15; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36"
)
CHROME_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) CriOS/141.0.0.0 Mobile/15E148 Safari/604.1"
)
OPERA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36 OPR/124.0.0.0"
)
CHROMEBOOK = (
    "Mozilla/5.0 (X11; CrOS x86_64 16328.55.0) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


@pytest.mark.parametrize(
    ("agent", "label"),
    [
        (CHROME_MAC, "Chrome · macOS"),
        (SAFARI_IPHONE, "Safari · iOS"),
        (SAFARI_MAC, "Safari · macOS"),
        (FIREFOX_WINDOWS, "Firefox · Windows"),
        (FIREFOX_LINUX, "Firefox · Linux"),
        (EDGE_WINDOWS, "Edge · Windows"),
        (CHROME_ANDROID, "Chrome · Android"),
        (SAMSUNG, "Samsung Internet · Android"),
        (CHROME_IOS, "Chrome · iOS"),
        (OPERA, "Opera · Windows"),
        (CHROMEBOOK, "Chrome · ChromeOS"),
        ("Python/3.14 websockets/17.1", "Python websockets"),
        ("curl/8.16.0", "curl"),
        ("", "Unknown device"),
        (None, "Unknown device"),
        ("something else entirely", "Unknown device"),
    ],
)
def test_device_labels(agent: str | None, label: str) -> None:
    assert device_label(agent) == label


def record(uid: str, sid: str, *, second: int = 0) -> SessionRecord:
    return SessionRecord(
        sid=sid,
        uid=uid,
        replica="api-a",
        connected_at=datetime(2026, 9, 26, 12, 0, second, tzinfo=UTC),
        agent=None,
        label="curl",
        ip="10.0.0.1",
        jti=f"jti-{sid}",
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_the_mirror_tracks_membership_changes_only() -> None:
    mirror = SessionMirror(ttl_s=30)
    assert mirror.put(record("u", "a"), revision=1)
    assert not mirror.put(record("u", "a"), revision=5)  # a refresh is not a change
    assert mirror.put(record("u", "b"), revision=2)
    assert {r.sid for r in mirror.sessions("u")} == {"a", "b"}
    assert mirror.remove("u.a") == "u"
    assert mirror.remove("u.a") is None
    assert [r.sid for r in mirror.sessions("u")] == ["b"]
    assert mirror.get("u.b") == record("u", "b")


def test_a_closed_session_cannot_be_brought_back_by_a_late_write() -> None:
    mirror = SessionMirror(ttl_s=30)
    mirror.put(record("u", "a"), revision=1)
    mirror.remove("u.a")
    assert not mirror.put(record("u", "a"), revision=3)
    assert mirror.sessions("u") == []


def test_admission_order_follows_the_first_revision_even_after_refreshes() -> None:
    mirror = SessionMirror(ttl_s=30)
    mirror.put(record("u", "old"), revision=3)
    mirror.put(record("u", "new"), revision=9)
    mirror.put(record("u", "old"), revision=12)  # refreshed after "new" registered
    mirror.put(record("other", "x"), revision=1)
    assert mirror.admitted_before("u.old") == 0
    assert mirror.admitted_before("u.new") == 1


def test_sessions_nobody_refreshes_expire_after_the_ttl() -> None:
    clock = Clock()
    mirror = SessionMirror(ttl_s=10, clock=clock)
    mirror.put(record("u", "gone"), revision=1)
    mirror.put(record("u", "alive"), revision=2)
    clock.now += 8
    mirror.put(record("u", "alive"), revision=3)
    clock.now += 4.5  # "gone" was last seen 12.5 s ago: past TTL + grace
    assert mirror.admitted_before("u.alive") == 0  # a stale entry no longer counts
    assert mirror.expire() == {"u"}
    assert [r.sid for r in mirror.sessions("u")] == ["alive"]
    assert mirror.expire() == set()


def test_expired_sessions_may_come_back_but_removed_ones_are_forgotten_later() -> None:
    clock = Clock()
    mirror = SessionMirror(ttl_s=10, clock=clock)
    mirror.put(record("u", "a"), revision=1)
    mirror.remove("u.b")
    clock.now += 13
    mirror.expire()
    assert mirror.put(record("u", "a"), revision=4)  # its replica came back and refreshed it
    assert not mirror.put(record("u", "b"), revision=5)  # still remembered as closed
    clock.now += 20
    mirror.expire()
    assert mirror.put(record("u", "b"), revision=6)  # tombstones do not live forever


class FakeWatcher:
    def __init__(self) -> None:
        self.updates: asyncio.Queue[Any] = asyncio.Queue()
        self.updates.put_nowait(None)  # initial values delivered

    def __aiter__(self) -> FakeWatcher:
        return self

    async def __anext__(self) -> Any:
        entry = await self.updates.get()
        if entry is StopAsyncIteration:
            raise StopAsyncIteration
        return entry

    async def stop(self) -> None:
        self.updates.put_nowait(StopAsyncIteration)


class FakeBucket:
    """The ``sessions`` bucket; ``echo=False`` simulates a watch that has fallen behind."""

    def __init__(self, *, echo: bool) -> None:
        self.echo = echo
        self.revision = 0
        self.values: dict[str, bytes] = {}
        self.watcher = FakeWatcher()

    async def status(self) -> Any:
        return SimpleNamespace(ttl=30.0)

    async def watchall(self) -> FakeWatcher:
        return self.watcher

    async def put(self, key: str, value: bytes) -> int:
        self.revision += 1
        self.values[key] = value
        if self.echo:
            self.watcher.updates.put_nowait(
                SimpleNamespace(key=key, value=value, revision=self.revision, operation=None)
            )
        return self.revision

    async def delete(self, key: str) -> bool:
        self.revision += 1
        self.values.pop(key, None)
        if self.echo:
            self.watcher.updates.put_nowait(
                SimpleNamespace(key=key, value=b"", revision=self.revision, operation="DEL")
            )
        return True


class FakeBroker:
    def __init__(self, bucket: FakeBucket) -> None:
        self.bucket = bucket
        self.published: list[tuple[str, bytes]] = []

    async def key_value(self, _: str) -> FakeBucket:
        return self.bucket

    async def publish(self, subject: str, payload: bytes = b"") -> None:
        self.published.append((subject, payload))


async def registry(bucket: FakeBucket, *, cap: int = 2) -> tuple[SessionRegistry, FakeBroker]:
    broker = FakeBroker(bucket)
    sessions = SessionRegistry(
        cast("JetStreamContext", broker),
        cast("NatsClient", broker),
        instance="api-a",
        max_per_user=cap,
    )
    await sessions.start()
    return sessions, broker


async def test_registration_beyond_the_cap_is_refused_and_leaves_nothing_behind() -> None:
    bucket = FakeBucket(echo=True)
    sessions, broker = await registry(bucket, cap=1)
    changes: list[str] = []
    sessions.on_change(changes.append)
    assert await sessions.register(record("u", "first", second=1))
    assert not await sessions.register(record("u", "second", second=2))
    assert [r.sid for r in sessions.sessions_of("u")] == ["first"]
    assert list(bucket.values) == ["u.first"]
    assert sessions.snapshot() == {"sessions": 1}
    joins = [p for s, p in broker.published if s == "live.ses.u" and b'"join"' in p]
    assert len(joins) == 1  # the refused session was never announced
    assert "u" in changes
    await sessions.close()


async def test_a_mirror_that_lags_behind_admits_rather_than_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions_module, "MIRROR_WAIT_S", 0.05)
    sessions, _ = await registry(FakeBucket(echo=False), cap=1)
    assert await sessions.register(record("u", "first"))
    assert await sessions.register(record("u", "second"))  # a soft limit when the watch lags
    await sessions.close()


async def test_closing_the_registry_releases_registrations_waiting_for_the_mirror() -> None:
    sessions, _ = await registry(FakeBucket(echo=False), cap=1)
    waiting = asyncio.create_task(sessions.register(record("u", "first")))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await sessions.close()
    assert await asyncio.wait_for(waiting, 1) is True


async def test_notices_from_other_replicas_update_the_mirror_ahead_of_the_watch() -> None:
    sessions, _ = await registry(FakeBucket(echo=False))
    changes: list[str] = []
    sessions.on_change(changes.append)
    user = uuid.UUID(int=5)
    remote = record(str(user), "remote")
    join = msgspec.json.encode(
        {"op": "join", "key": remote.key, "revision": 7, "session": msgspec.to_builtins(remote)}
    )
    sessions.apply_notice(user, join)
    assert [r.sid for r in sessions.sessions_of(user)] == ["remote"]
    sessions.apply_notice(user, b'{"op":"leave","key":"%s"}' % remote.key.encode())
    assert sessions.sessions_of(user) == []
    sessions.apply_notice(user, b'{"op":"leave","key":"someone-else.remote"}')
    sessions.apply_notice(user, b"not json")
    assert changes == [str(user), str(user)]
    await sessions.close()
