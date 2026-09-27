"""In-process token-bucket rate limiting per client address (used for sign-in).

Each client gets a bucket of ``burst`` tokens refilled continuously at ``rate_per_minute``; a
request takes one token or is refused with the time until the next token. Memory stays bounded:
buckets live in an LRU map, a bucket that has refilled completely is indistinguishable from a new
one and is dropped, and beyond ``max_clients`` the least recently seen client is forgotten.

IPv6 clients are keyed by their /64 network, because a single host usually controls a whole /64
and could otherwise mint a fresh identity per request. Limits are per replica; with ``n`` replicas
behind the edge a client gets at most ``n`` times the rate, which is acceptable for a guard whose
job is to keep one client from monopolising sign-ins, not to meter usage exactly.
"""

from __future__ import annotations

import ipaddress
from collections import OrderedDict
from dataclasses import dataclass

from perimeter.domain.clock import SYSTEM_CLOCK, Clock


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


def client_key(host: str | None) -> str:
    """Rate-limit identity of a client address."""
    if not host:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


class RateLimiter:
    def __init__(
        self,
        *,
        rate_per_minute: float,
        burst: int | None = None,
        max_clients: int = 10_000,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if rate_per_minute <= 0:
            msg = "rate_per_minute must be positive"
            raise ValueError(msg)
        self._rate_per_s = rate_per_minute / 60.0
        self._burst = float(burst if burst is not None else max(1, round(rate_per_minute)))
        self._max_clients = max_clients
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def acquire(self, key: str) -> float:
        """Take a token for ``key``: 0.0 when allowed, else seconds until one is available."""
        now = self._clock.monotonic()
        self._forget_idle(now)
        bucket = self._buckets.pop(key, None)
        if bucket is None:
            bucket = _Bucket(self._burst, now)
        else:
            refill = (now - bucket.updated_at) * self._rate_per_s
            bucket.tokens = min(self._burst, bucket.tokens + refill)
            bucket.updated_at = now
        self._buckets[key] = bucket
        if len(self._buckets) > self._max_clients:
            self._buckets.popitem(last=False)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self._rate_per_s

    def _forget_idle(self, now: float) -> None:
        """Drop least recently used buckets that have refilled completely."""
        while self._buckets:
            key, bucket = next(iter(self._buckets.items()))
            if bucket.tokens + (now - bucket.updated_at) * self._rate_per_s < self._burst:
                return
            del self._buckets[key]
