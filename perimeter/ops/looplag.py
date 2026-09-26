"""Event-loop lag: how late a coroutine wakes up compared to when it asked to.

If anything blocks the loop (CPU-heavy work, a synchronous call), every coroutine in the process
is delayed by the same amount; this monitor measures that delay continuously so the claim "nothing
blocks the loop" is a number on a dashboard rather than an assertion in a README.
"""

from __future__ import annotations

import asyncio
from collections import deque

from prometheus_client import Gauge, Histogram

LOOP_LAG = Histogram(
    "perimeter_event_loop_lag_seconds",
    "Delay between a scheduled wake-up and the actual one",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
LOOP_LAG_MAX = Gauge("perimeter_event_loop_lag_max_seconds", "Worst lag over the last minute")


class LoopLagMonitor:
    def __init__(self, *, interval_s: float = 0.25, window: int = 240) -> None:
        self._interval_s = interval_s
        self._samples: deque[float] = deque(maxlen=window)

    @property
    def worst(self) -> float:
        return max(self._samples, default=0.0)

    def percentile(self, q: float) -> float:
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        index = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
        return ordered[index]

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            started = loop.time()
            await asyncio.sleep(self._interval_s)
            lag = max(0.0, loop.time() - started - self._interval_s)
            self._samples.append(lag)
            LOOP_LAG.observe(lag)
            LOOP_LAG_MAX.set(self.worst)
