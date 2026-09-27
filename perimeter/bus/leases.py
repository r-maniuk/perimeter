"""Leases on top of a NATS key-value bucket whose entries expire after the bucket TTL.

* ``acquire`` creates the key only if it does not exist (compare-and-set on "no revision").
* ``renew`` rewrites it only if nobody else wrote it since (compare-and-set on the revision we
  hold); a failed renew means the lease is lost, full stop.
* An owner that stops renewing (crash, pause, network split) loses the key when the bucket TTL
  expires, and anyone may then acquire it.

Leases alone cannot stop a paused owner from waking up and writing after it lost the lease, so each
lease also yields a *fencing token*: the pair (bucket generation, key revision). Revisions grow with
every acquisition or renewal; the generation (the bucket's creation time) grows if the broker was
wiped and the bucket recreated, so pairs compared in order strictly increase across both. Writers
present the token to the database, which rejects anything older than the newest token it has seen.
Comparing the pair as a row keeps both halves full 64-bit integers: nothing to pack, nothing to
overflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from nats.js.errors import (
    BadRequestError,
    KeyNotFoundError,
    KeyValueError,
    KeyWrongLastSequenceError,
    NoKeysError,
)
from nats.js.kv import KeyValue

_CREATE_ATTEMPTS = 3


class LeaseLost(Exception):  # noqa: N818 - a state, not a failure of the caller
    """Somebody else holds the key now."""


class FencingToken(NamedTuple):
    """Ordered pair: a newer bucket beats any revision of an older one."""

    generation: int
    revision: int


@dataclass(frozen=True, slots=True)
class Lease:
    key: str
    owner: str
    revision: int
    generation: int

    @property
    def token(self) -> FencingToken:
        return FencingToken(self.generation, self.revision)


def generation_of(created: datetime | None) -> int:
    """A bucket recreated later always gets a larger generation (its creation second)."""
    return int(created.timestamp()) if created is not None else 0


class LeaseBucket:
    def __init__(self, kv: KeyValue, *, generation: int) -> None:
        self._kv = kv
        self._generation = generation

    async def acquire(self, key: str, owner: str) -> Lease | None:
        # ``KeyValue.create`` answers "taken" with a second round trip that reads the key back (to
        # tell a live key from a delete marker). When the holder's entry expires between the two,
        # that read finds nothing and raises KeyNotFoundError: the key has just become free, so
        # create it again. A new holder in the meantime answers "taken" like any other.
        for _ in range(_CREATE_ATTEMPTS):
            try:
                revision = await self._kv.create(key, owner.encode())
            except KeyWrongLastSequenceError:
                return None
            except KeyNotFoundError:
                continue
            return Lease(key, owner, revision, self._generation)
        return None

    async def renew(self, lease: Lease) -> Lease:
        try:
            revision = await self._kv.update(lease.key, lease.owner.encode(), last=lease.revision)
        except KeyValueError as exc:
            raise LeaseLost(lease.key) from exc
        return Lease(lease.key, lease.owner, revision, lease.generation)

    async def release(self, lease: Lease) -> None:
        """Give the key back early so the next owner does not wait for the TTL.

        The delete is conditional on our revision: a lease that was already lost must never
        delete the new owner's key.
        """
        try:
            await self._kv.delete(lease.key, last=lease.revision)
        except KeyValueError, BadRequestError:  # the key moved on: not ours to delete
            return

    async def holder(self, key: str) -> str | None:
        try:
            entry = await self._kv.get(key)
        except KeyValueError:
            return None
        return entry.value.decode() if entry.value else None

    async def heartbeat(self, key: str, value: bytes) -> None:
        await self._kv.put(key, value)

    async def remove(self, key: str) -> None:
        """Delete a heartbeat key right away (a member leaving) instead of letting it expire."""
        await self._kv.delete(key)

    async def keys(self, prefix: str) -> list[str]:
        """Live keys starting with ``prefix`` (expired and deleted keys are not listed)."""
        try:
            keys = await self._kv.keys()
        except NoKeysError:
            return []
        return sorted(key for key in keys if key.startswith(prefix))
