from __future__ import annotations

import pytest

from perimeter.api.ratelimit import RateLimiter, client_key
from perimeter.domain.clock import ManualClock


def test_burst_then_steady_rate() -> None:
    clock = ManualClock()
    limiter = RateLimiter(rate_per_minute=6, clock=clock)  # burst 6, one token per 10 s
    assert [limiter.acquire("a") for _ in range(6)] == [0.0] * 6
    wait = limiter.acquire("a")
    assert wait == pytest.approx(10.0)
    clock.advance(5)
    assert limiter.acquire("a") == pytest.approx(5.0)  # refused attempts do not consume tokens
    clock.advance(5)
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") > 0


def test_clients_are_limited_independently() -> None:
    limiter = RateLimiter(rate_per_minute=1, clock=ManualClock())
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") > 0
    assert limiter.acquire("b") == 0.0


def test_explicit_burst() -> None:
    limiter = RateLimiter(rate_per_minute=60, burst=2, clock=ManualClock())
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") == 0.0
    assert limiter.acquire("a") == pytest.approx(1.0)


def test_memory_is_bounded_by_the_client_cap() -> None:
    limiter = RateLimiter(rate_per_minute=1, max_clients=3, clock=ManualClock())
    for key in "abcde":
        limiter.acquire(key)
    assert len(limiter) == 3


def test_refilled_buckets_are_forgotten() -> None:
    clock = ManualClock()
    limiter = RateLimiter(rate_per_minute=60, clock=clock)
    for key in ("a", "b", "c"):
        limiter.acquire(key)
    clock.advance(0.5)
    limiter.acquire("d")
    assert len(limiter) == 4  # nobody refilled yet
    clock.advance(2)
    limiter.acquire("e")
    assert len(limiter) == 1  # everyone else was full again, indistinguishable from new


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(rate_per_minute=0)


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("::ffff:198.51.100.4", "198.51.100.4"),
        ("testclient", "testclient"),
        (None, "unknown"),
    ],
)
def test_client_keys(host: str | None, key: str) -> None:
    assert client_key(host) == key
