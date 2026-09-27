"""Heartbeat numbers: rates and percentiles per window, and the payload's shape."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Gauge
from pydantic import SecretStr

from perimeter.config import DatabaseSettings, NatsSettings, TelemetrySettings, load_settings
from perimeter.domain.clock import ManualClock
from perimeter.engine.metrics import EngineStats, gauge_value, heartbeat_payload, percentile
from perimeter.engine.service import lease_timeout, pool_size

ENGINE_KEYS = {
    "loop_lag_p99_ms",
    "partitions",
    "reports_rate",
    "batches_rate",
    "batch_p50_ms",
    "batch_p99_ms",
    "commit_lag_p99_ms",
    "alerts_rate",
    "late_rate",
    "relay_backlog",
}


def test_a_window_reports_per_second_rates_and_percentiles_then_starts_over() -> None:
    clock = ManualClock()
    stats = EngineStats(clock)
    stats.record(reports=10, late=1, alerts=2, batch_s=0.004, lags_ms=[5.0] * 10)
    stats.record(reports=30, late=0, alerts=0, batch_s=0.010, lags_ms=[7.0] * 29 + [250.0])
    clock.advance(2.0)
    window = stats.window()
    assert window == {
        "reports_rate": 20.0,
        "batches_rate": 1.0,
        "batch_p50_ms": 4.0,
        "batch_p99_ms": 10.0,
        "commit_lag_p99_ms": 250.0,
        "alerts_rate": 1.0,
        "late_rate": 0.5,
    }
    clock.advance(1.0)
    assert set(stats.window().values()) == {0.0}


def test_latency_samples_are_bounded() -> None:
    stats = EngineStats(ManualClock(), max_samples=4)
    stats.record(reports=1, late=0, alerts=0, batch_s=0.001, lags_ms=[1.0, 2.0, 3.0, 4.0, 900.0])
    assert stats.window()["commit_lag_p99_ms"] == 900.0  # the newest samples are kept


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 0.99) == 0.0
    assert percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert percentile([float(i) for i in range(101)], 0.99) == 99.0


def test_the_heartbeat_carries_every_engine_field() -> None:
    stats = EngineStats(ManualClock())
    payload = heartbeat_payload(
        partitions={7, 1, 3}, window=stats.window(), loop_lag_p99_ms=1.23456, relay_backlog=4
    )
    assert set(payload) == ENGINE_KEYS
    assert payload["partitions"] == [1, 3, 7]
    assert payload["loop_lag_p99_ms"] == 1.23
    assert payload["relay_backlog"] == 4


def test_gauge_value_reads_the_current_value() -> None:
    gauge = Gauge("test_backlog", "test", registry=CollectorRegistry())
    assert gauge_value(gauge) == 0.0
    gauge.set(12)
    assert gauge_value(gauge) == 12.0


def test_the_pool_fits_a_worker_per_partition_plus_the_sweeper_and_headroom() -> None:
    small = load_settings(
        database=DatabaseSettings(pool_size=5, password=SecretStr("x")),
        telemetry=TelemetrySettings(partitions=16),
    )
    assert pool_size(small) == 18
    large = load_settings(
        database=DatabaseSettings(pool_size=40, password=SecretStr("x")),
        telemetry=TelemetrySettings(partitions=16),
    )
    assert pool_size(large) == 40


def test_lease_operations_time_out_well_within_a_round() -> None:
    settings = load_settings(nats=NatsSettings(request_timeout_s=5.0))
    assert lease_timeout(settings, 6.0) == 1.0  # a round is 2 s
    assert lease_timeout(settings, 60.0) == 5.0  # never longer than the broker timeout
    assert lease_timeout(settings, 0.6) == 0.25  # never absurdly short
