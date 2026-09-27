"""Which position subjects this replica subscribes to, and which sessions each frame goes to.

Every session holds a set of quadkey prefixes (the tiles covering its viewport). The replica must
receive every frame below any held prefix, exactly once, while subscribing to as little as
possible. :class:`InterestTrie` keeps the *minimal cover*: the held prefixes that have no held
ancestor. Subscribing ``pos.<prefix>.>`` for exactly those prefixes receives every wanted frame,
and because no cover element is an ancestor of another, each frame matches exactly one
subscription. Holding an ancestor makes its descendants redundant (they are unsubscribed);
releasing it brings back the descendants that are still held.

Dispatch walks the frame tile's ancestors (at most ``zoom + 1`` dictionary lookups) and collects
the sessions holding each of them. The structure is pure; :class:`TileSubscriptions` is the thin
adapter that keeps real NATS subscriptions equal to the cover.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field

import structlog
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from nats.errors import Error as NatsError

from perimeter.api.live import metrics
from perimeter.domain import tiles

log = structlog.get_logger(__name__)

RETRY_DELAY_S = 1.0


@dataclass(frozen=True, slots=True)
class InterestChange:
    """Outcome of one session's update: its own prefix delta and the net change of the cover."""

    added: frozenset[str]
    removed: frozenset[str]
    subscribe: frozenset[str]
    unsubscribe: frozenset[str]


@dataclass(slots=True)
class _Node[K: Hashable]:
    holders: set[K] = field(default_factory=set)
    children: dict[str, _Node[K]] = field(default_factory=dict)


@dataclass(slots=True)
class _CoverDelta:
    """Net cover changes of one update: entering and leaving the same prefix cancels out."""

    subscribe: set[str] = field(default_factory=set)
    unsubscribe: set[str] = field(default_factory=set)

    def enter(self, prefix: str) -> None:
        if prefix in self.unsubscribe:
            self.unsubscribe.discard(prefix)
        else:
            self.subscribe.add(prefix)

    def leave(self, prefix: str) -> None:
        if prefix in self.subscribe:
            self.subscribe.discard(prefix)
        else:
            self.unsubscribe.add(prefix)


class InterestTrie[K: Hashable]:
    def __init__(self) -> None:
        self._root: _Node[K] = _Node()
        self._held: dict[K, frozenset[str]] = {}
        self._cover: set[str] = set()

    @property
    def cover(self) -> frozenset[str]:
        return frozenset(self._cover)

    def covers(self, prefix: str) -> bool:
        """Whether ``prefix`` is currently an element of the minimal cover."""
        return prefix in self._cover

    def prefixes(self, key: K) -> frozenset[str]:
        return self._held.get(key, frozenset())

    def update(self, key: K, prefixes: Iterable[str]) -> InterestChange:
        """Make ``key`` hold exactly ``prefixes``."""
        new = frozenset(prefixes)
        old = self._held.get(key, frozenset())
        delta = _CoverDelta()
        for prefix in sorted(old - new):
            self._release(prefix, key, delta)
        for prefix in sorted(new - old):
            self._hold(prefix, key, delta)
        if new:
            self._held[key] = new
        else:
            self._held.pop(key, None)
        return InterestChange(
            added=new - old,
            removed=old - new,
            subscribe=frozenset(delta.subscribe),
            unsubscribe=frozenset(delta.unsubscribe),
        )

    def remove(self, key: K) -> InterestChange:
        return self.update(key, ())

    def targets(self, quadkey: str) -> set[K]:
        """Keys holding ``quadkey`` or any of its ancestors (each key once)."""
        node = self._root
        found: set[K] = set(node.holders)
        for digit in quadkey:
            child = node.children.get(digit)
            if child is None:
                break
            node = child
            if node.holders:
                found |= node.holders
        return found

    def _path(self, prefix: str) -> list[_Node[K]]:
        """Existing nodes from the root towards ``prefix`` (shorter if the path ends early)."""
        node = self._root
        path = [node]
        for digit in prefix:
            child = node.children.get(digit)
            if child is None:
                break
            node = child
            path.append(node)
        return path

    def _hold(self, prefix: str, key: K, delta: _CoverDelta) -> None:
        node = self._root
        covered = False
        for digit in prefix:
            covered = covered or bool(node.holders)
            node = node.children.setdefault(digit, _Node())
        first = not node.holders
        node.holders.add(key)
        if not first or covered:
            return
        for descendant in self._top_holders_below(node, prefix):
            self._cover.discard(descendant)
            delta.leave(descendant)
        self._cover.add(prefix)
        delta.enter(prefix)

    def _release(self, prefix: str, key: K, delta: _CoverDelta) -> None:
        path = self._path(prefix)
        if len(path) != len(prefix) + 1:
            return
        node = path[-1]
        node.holders.discard(key)
        if not node.holders and prefix in self._cover:
            self._cover.discard(prefix)
            delta.leave(prefix)
            for descendant in self._top_holders_below(node, prefix):
                self._cover.add(descendant)
                delta.enter(descendant)
        for depth in range(len(prefix), 0, -1):  # prune empty branches bottom-up
            current = path[depth]
            if current.holders or current.children:
                break
            del path[depth - 1].children[prefix[depth - 1]]

    def _top_holders_below(self, node: _Node[K], prefix: str) -> Iterator[str]:
        """Held prefixes strictly below ``prefix`` that have no held ancestor below it."""
        stack = [(node, prefix)]
        while stack:
            current, key = stack.pop()
            for digit, child in current.children.items():
                if child.holders:
                    yield key + digit
                else:
                    stack.append((child, key + digit))


class TileSubscriptions[K: Hashable]:
    """Keeps this replica's position subscriptions equal to the trie's minimal cover.

    Changes are applied by one background task at a time, new subscriptions before removals, so
    an area changing hands between a prefix and its ancestor is never left uncovered. A frame is
    dispatched only by the subscription whose prefix is currently in the cover, so a frame that
    matches an old and a new subscription during such a hand-over is still delivered once.
    """

    def __init__(
        self,
        nc: NatsClient,
        trie: InterestTrie[K],
        *,
        leaf_zoom: int,
        on_frame: Callable[[str, bytes], None],
    ) -> None:
        self._nc = nc
        self._trie = trie
        self._leaf_zoom = leaf_zoom
        self._on_frame = on_frame
        self._subscriptions: dict[str, Subscription] = {}
        self._dirty = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._subscriptions)

    def sync(self) -> None:
        """Schedule reconciliation with the current cover."""
        self._dirty.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._reconcile_while_dirty(), name="live-tiles")

    async def settled(self) -> None:
        """Wait until the subscriptions match the cover as of now (tests, diagnostics)."""
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)

    async def _reconcile_while_dirty(self) -> None:
        while self._dirty.is_set():
            self._dirty.clear()
            try:
                await self._reconcile()
            except Exception:  # the broker may be reconnecting: keep the desired state, retry
                log.warning("live.tile_subscriptions_failed", exc_info=True)
                self._dirty.set()
                await asyncio.sleep(RETRY_DELAY_S)

    async def _reconcile(self) -> None:
        for prefix in sorted(self._trie.cover - self._subscriptions.keys()):
            subject = tiles.subscription_for(prefix, self._leaf_zoom)
            self._subscriptions[prefix] = await self._nc.subscribe(
                subject, cb=self._dispatcher(prefix)
            )
        for prefix in [p for p in self._subscriptions if not self._trie.covers(p)]:
            subscription = self._subscriptions.pop(prefix)
            with suppress(NatsError):
                await subscription.unsubscribe()
        metrics.TILE_SUBSCRIPTIONS.set(len(self._subscriptions))

    def _dispatcher(self, prefix: str) -> Callable[[Msg], Awaitable[None]]:
        trie = self._trie
        on_frame = self._on_frame

        async def dispatch(msg: Msg) -> None:
            if trie.covers(prefix):
                on_frame(msg.subject, msg.data)

        return dispatch

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        for subscription in self._subscriptions.values():
            with suppress(NatsError):
                await subscription.unsubscribe()
        self._subscriptions.clear()
        metrics.TILE_SUBSCRIPTIONS.set(0)
