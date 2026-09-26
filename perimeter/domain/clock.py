"""Injectable time source so time-dependent logic is testable without sleeping."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that only moves when told to (tests)."""

    def __init__(self, now_ms: int = 1_790_000_000_000) -> None:
        self._now_ms = now_ms
        self._monotonic = 0.0

    def now_ms(self) -> int:
        return self._now_ms

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now_ms += round(seconds * 1000)
        self._monotonic += seconds


SYSTEM_CLOCK: Clock = SystemClock()
