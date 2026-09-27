"""Metrics of the live channel: Prometheus series plus the per-second rates of the heartbeat."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from prometheus_client import Counter, Gauge, Histogram

SESSIONS = Gauge("perimeter_live_sessions", "Live WebSocket sessions served by this replica")
SENT_MESSAGES = Counter(
    "perimeter_live_sent_messages_total", "WebSocket messages sent to live sessions", ["kind"]
)
SENT_BYTES = Counter("perimeter_live_sent_bytes_total", "Payload bytes sent to live sessions")
SEND_SECONDS = Histogram(
    "perimeter_live_send_seconds",
    "Time for a live socket to accept one message",
    buckets=(0.0001, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.1, 0.5, 1.0, 5.0),
)
DROPPED_FRAMES = Counter(
    "perimeter_live_dropped_frames_total",
    "Frames dropped for sessions that fell behind (they resync or resume)",
    ["lane"],
)
CLOSED = Counter(
    "perimeter_live_closed_total", "Live sessions ended, by WebSocket close code", ["code"]
)
TILE_SUBSCRIPTIONS = Gauge(
    "perimeter_live_tile_subscriptions",
    "Position subjects this replica subscribes to (the minimal cover of all viewports)",
)
USER_FEEDS = Gauge(
    "perimeter_live_user_feeds", "Users with at least one live session on this replica"
)
EVENTS = Counter(
    "perimeter_live_events_total",
    "User events taken from the broker, by how they arrived",
    ["path"],  # live | healed | replayed
)
HEAL_FAILURES = Counter(
    "perimeter_live_heal_failures_total", "Event gaps that could not be filled from the stream"
)
RESUMES = Counter("perimeter_live_resumes_total", "Live session starts, by resume mode", ["mode"])
SNAPSHOTS = Counter(
    "perimeter_live_snapshots_total",
    "Viewport snapshot requests, by outcome",
    ["result"],  # cached | shared | queried | failed
)
SNAPSHOT_SECONDS = Histogram(
    "perimeter_live_snapshot_seconds",
    "Snapshot query and encoding time for one tile",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


@dataclass(slots=True)
class Counters:
    """Plain running totals behind the heartbeat's rates (Prometheus keeps its own series)."""

    sent: int = 0
    dropped: int = 0


class Rates:
    """Per-second rates of :class:`Counters` between two consecutive reads."""

    def __init__(self, counters: Counters, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._counters = counters
        self._clock = clock
        self._at = clock()
        self._sent = counters.sent
        self._dropped = counters.dropped

    def read(self) -> dict[str, float]:
        now = self._clock()
        elapsed = max(now - self._at, 1e-6)
        rates = {
            "live_out_rate": round((self._counters.sent - self._sent) / elapsed, 1),
            "live_drops_rate": round((self._counters.dropped - self._dropped) / elapsed, 1),
        }
        self._at, self._sent, self._dropped = now, self._counters.sent, self._counters.dropped
        return rates
