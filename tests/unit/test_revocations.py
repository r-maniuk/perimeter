"""The replica's mirror of revoked tokens: it fails closed, recovers, and stays bounded."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from perimeter.api import security
from perimeter.api.security import RevocationList, RevocationsUnavailable
from tests.support import eventually


@dataclass(frozen=True)
class Entry:
    key: str
    operation: str | None = None


class Watcher:
    """Replays the bucket's current keys, marks the end of them, then waits for updates."""

    def __init__(self, keys: list[str], *, stalled: bool = False) -> None:
        self._keys = keys
        self._stalled = stalled
        self.stopped = False

    def __aiter__(self) -> AsyncIterator[Entry | None]:
        return self._entries()

    async def _entries(self) -> AsyncIterator[Entry | None]:
        if self._stalled:
            await asyncio.Event().wait()
        for key in self._keys:
            yield Entry(key)
        yield None
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stopped = True


class Bucket:
    def __init__(self, *outcomes: Watcher | Exception) -> None:
        self.outcomes = deque(outcomes)
        self.keys: list[str] = []
        self.watches = 0

    async def watchall(self) -> Watcher:
        self.watches += 1
        outcome = self.outcomes.popleft() if self.outcomes else Watcher(list(self.keys))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def put(self, key: str, value: bytes) -> int:
        self.keys.append(key)
        return len(self.keys)


def mirror(bucket: Bucket, *, keep_s: float = 3_600) -> RevocationList:
    return RevocationList(bucket, keep_s=keep_s)  # type: ignore[arg-type]


async def test_a_replica_that_cannot_read_the_revocations_refuses_to_start() -> None:
    revocations = mirror(Bucket(Watcher([], stalled=True)))
    with pytest.raises(RevocationsUnavailable):
        await revocations.start(sync_timeout_s=0.2)


async def test_a_lost_watch_is_started_again() -> None:
    bucket = Bucket(ConnectionResetError("broker went away"), Watcher(["jti-1"]))
    revocations = mirror(bucket)
    await revocations.start(sync_timeout_s=5)
    assert revocations.is_revoked("jti-1")
    assert bucket.watches == 2
    await revocations.close()


async def test_the_bucket_is_read_anew_and_expired_revocations_are_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security, "REWATCH_EVERY_S", 0.1)
    bucket = Bucket(Watcher(["jti-old"]))
    revocations = mirror(bucket, keep_s=0.3)
    await revocations.start(sync_timeout_s=5)
    assert revocations.is_revoked("jti-old")
    # A revocation the watch never delivered (it stalled) still arrives with the next full read,
    # and one the bucket expired is forgotten once no token it concerns can still be valid.
    bucket.keys = ["jti-new"]
    await eventually(lambda: revocations.is_revoked("jti-new"), within=3)
    await eventually(lambda: not revocations.is_revoked("jti-old"), within=3)
    assert len(revocations) == 1
    await revocations.close()


async def test_listener_queues_are_bounded_and_keep_the_newest() -> None:
    revocations = mirror(Bucket(Watcher([])))
    await revocations.start(sync_timeout_s=5)
    queue = revocations.subscribe()
    for n in range(security.LISTENER_QUEUE_MAX + 5):
        await revocations.revoke(f"jti-{n}")
    assert queue.qsize() == security.LISTENER_QUEUE_MAX
    assert queue.get_nowait() == "jti-5"
    await revocations.close()
