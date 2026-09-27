"""Engine metrics.

Two audiences, two shapes. The Prometheus series (``perimeter_engine_*``, scraped from
``ENGINE_METRICS_PORT``) are cumulative and answer "what happened over time". The one-second
heartbeat on ``sys.metrics.engine.<instance>`` answers "what is happening right now" for the ops
view, so :class:`EngineStats` keeps only the counts and latency samples since the previous
heartbeat and turns them into per-second rates and percentiles.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from perimeter.domain.clock import SYSTEM_CLOCK, Clock

LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SIZE_BUCKETS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000)

BATCHES = Counter("perimeter_engine_batches_total", "Telemetry batches by outcome", ["outcome"])
REPORTS = Counter("perimeter_engine_reports_total", "Reports in committed batches", ["outcome"])
POISON = Counter(
    "perimeter_engine_poison_total",
    "Messages terminated because they can never be applied",
    ["reason"],
)
ALERTS = Counter("perimeter_engine_alerts_total", "Alerts raised", ["kind"])
RECOVERED = Counter(
    "perimeter_engine_recovered_reports_total",
    "Reports a new partition owner applied from its predecessor's unacknowledged messages",
)
BATCH_SIZE = Histogram(
    "perimeter_engine_batch_size", "Reports per committed batch", buckets=SIZE_BUCKETS
)
BATCH_SECONDS = Histogram(
    "perimeter_engine_batch_seconds",
    "Time to apply one batch, from the first statement to the commit",
    buckets=LATENCY_BUCKETS,
)
COMMIT_LAG = Histogram(
    "perimeter_engine_commit_lag_seconds",
    "Delay from receipt by the api to the engine's commit, per accepted report",
    buckets=LATENCY_BUCKETS,
)
PARTITIONS_OWNED = Gauge(
    "perimeter_engine_partitions_owned", "Telemetry partitions this instance consumes"
)
LEASE_EVENTS = Counter(
    "perimeter_engine_lease_events_total", "Partition lease transitions", ["event"]
)
FRAMES = Counter("perimeter_engine_frames_published_total", "Live position frames published")
FRAME_BYTES = Counter("perimeter_engine_frame_bytes_total", "Bytes of live position frames")
PULSES = Counter("perimeter_engine_pulses_published_total", "Occupancy pulses published")
PUBLISH_FAILURES = Counter(
    "perimeter_engine_publish_failures_total",
    "Core NATS publishes (frames, pulses) that failed",
    ["kind"],
)


def percentile(ordered: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted sequence (0 when empty)."""
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


class EngineStats:
    """Activity since the previous heartbeat, reported as rates and percentiles.

    Latency samples are kept in bounded windows: under a burst the oldest samples of the interval
    are dropped rather than letting memory grow with the load.
    """

    def __init__(self, clock: Clock = SYSTEM_CLOCK, *, max_samples: int = 8_192) -> None:
        self._clock = clock
        self._since = clock.monotonic()
        self._reports = 0
        self._batches = 0
        self._alerts = 0
        self._late = 0
        self._batch_ms: deque[float] = deque(maxlen=max_samples)
        self._lag_ms: deque[float] = deque(maxlen=max_samples)

    def record(
        self,
        *,
        reports: int,
        late: int,
        alerts: int,
        batch_s: float,
        lags_ms: Iterable[float],
    ) -> None:
        self._batches += 1
        self._reports += reports
        self._late += late
        self._alerts += alerts
        self._batch_ms.append(batch_s * 1000)
        self._lag_ms.extend(lags_ms)

    def window(self) -> dict[str, float]:
        """Rates and percentiles since the last call; starts a new window."""
        now = self._clock.monotonic()
        elapsed = max(now - self._since, 1e-6)
        batch_ms = sorted(self._batch_ms)
        lag_ms = sorted(self._lag_ms)
        result = {
            "reports_rate": round(self._reports / elapsed, 1),
            "batches_rate": round(self._batches / elapsed, 1),
            "batch_p50_ms": round(percentile(batch_ms, 0.50), 2),
            "batch_p99_ms": round(percentile(batch_ms, 0.99), 2),
            "commit_lag_p99_ms": round(percentile(lag_ms, 0.99), 1),
            "alerts_rate": round(self._alerts / elapsed, 2),
            "late_rate": round(self._late / elapsed, 1),
        }
        self._since = now
        self._reports = self._batches = self._alerts = self._late = 0
        self._batch_ms.clear()
        self._lag_ms.clear()
        return result


def heartbeat_payload(
    *,
    partitions: Iterable[int],
    window: Mapping[str, float],
    loop_lag_p99_ms: float,
    relay_backlog: int,
) -> dict[str, Any]:
    """The engine part of a ``sys.metrics.engine.<instance>`` heartbeat."""
    return {
        "loop_lag_p99_ms": round(loop_lag_p99_ms, 2),
        "partitions": sorted(partitions),
        **window,
        "relay_backlog": relay_backlog,
    }


def gauge_value(gauge: Gauge) -> float:
    """Current value of an unlabelled gauge (e.g. the relay backlog set by the sweeper)."""
    for family in gauge.collect():
        for sample in family.samples:
            return sample.value
    return 0.0
