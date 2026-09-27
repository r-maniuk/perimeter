from __future__ import annotations

import asyncio
import math
from typing import Any
from unittest.mock import MagicMock

import pytest
from nats.js.api import ConsumerConfig, ConsumerInfo, SequenceInfo

from perimeter.api.ingest.admission import (
    RETRY_AFTER_MAX_S,
    RETRY_AFTER_MIN_S,
    RETRY_AFTER_UNKNOWN_S,
    Admission,
    AdmissionController,
    AdmissionState,
    Backlog,
)
from perimeter.config import IngestSettings
from perimeter.domain.clock import ManualClock
from tests.support import eventually


def sample(pending: int, at: float, **acked: int) -> Backlog:
    return Backlog(pending=pending, acked=acked, at=at)


def test_it_fails_closed_until_the_first_measurement() -> None:
    admission = Admission(high=100, low=40)
    assert admission.state is AdmissionState.SHEDDING
    assert admission.observe(sample(0, 0.0))
    assert admission.admitting


def test_hysteresis_between_the_watermarks() -> None:
    admission = Admission(high=100, low=40)
    admission.observe(sample(10, 0.0))
    states = []
    for pending in (99, 100, 99, 41, 40, 99, 100):
        admission.observe(sample(pending, 1.0))
        states.append(admission.state)
    open_, shed = AdmissionState.OPEN, AdmissionState.SHEDDING
    assert states == [open_, shed, shed, shed, open_, open_, shed]


def test_an_unmeasurable_backlog_sheds_and_recovers_only_below_low() -> None:
    admission = Admission(high=100, low=40)
    admission.observe(sample(10, 0.0))
    assert admission.observe(None)
    assert not admission.admitting
    assert admission.backlog is None
    assert admission.retry_after_s() == RETRY_AFTER_UNKNOWN_S
    admission.observe(sample(60, 1.0))  # between the watermarks: stay cautious
    assert not admission.admitting
    admission.observe(sample(40, 2.0))
    assert admission.admitting


def test_drain_rate_follows_acknowledgements_and_sets_the_retry_hint() -> None:
    admission = Admission(high=1_000, low=100, smoothing=1.0)
    admission.observe(sample(2_000, 0.0, a=0, b=0))
    admission.observe(sample(1_900, 1.0, a=150, b=50))  # 200 acknowledged in one second
    assert admission.drain_rate == pytest.approx(200.0)
    assert not admission.admitting
    assert admission.retry_after_s() == math.ceil((1_900 - 100) / 200)


def test_retry_hint_is_clamped() -> None:
    admission = Admission(high=1_000, low=0, smoothing=1.0)
    admission.observe(sample(5_000, 0.0, a=0))
    admission.observe(sample(5_000, 1.0, a=0))  # nothing drains
    assert admission.retry_after_s() == RETRY_AFTER_MAX_S
    admission.observe(sample(5_000, 2.0, a=1_000_000))  # drains a lot very quickly
    assert admission.retry_after_s() == RETRY_AFTER_MIN_S
    admission.observe(sample(10**9, 3.0, a=1_000_001))
    assert admission.retry_after_s() == RETRY_AFTER_MAX_S


def test_a_recreated_consumer_does_not_count_as_negative_progress() -> None:
    admission = Admission(high=1_000, low=10, smoothing=1.0)
    admission.observe(sample(0, 0.0, a=5_000, b=100))
    admission.observe(sample(0, 1.0, a=10, b=200, c=30))  # a reset, c is new
    assert admission.drain_rate == pytest.approx(100.0)


def test_rate_is_smoothed_and_ignores_samples_without_elapsed_time() -> None:
    admission = Admission(high=1_000, low=10, smoothing=0.5)
    admission.observe(sample(0, 0.0, a=0))
    admission.observe(sample(0, 1.0, a=100))
    admission.observe(sample(0, 1.0, a=500))  # same instant: no rate update
    assert admission.drain_rate == pytest.approx(100.0)
    admission.observe(sample(0, 2.0, a=800))
    assert admission.drain_rate == pytest.approx(200.0)


@pytest.mark.parametrize(("high", "low"), [(10, 10), (10, 20), (10, -1)])
def test_watermarks_are_validated(high: int, low: int) -> None:
    with pytest.raises(ValueError, match="watermarks"):
        Admission(high=high, low=low)


def test_smoothing_is_validated() -> None:
    with pytest.raises(ValueError, match="smoothing"):
        Admission(high=10, low=1, smoothing=0)


def consumer(name: str, *, pending: int, ack_pending: int, ack_floor: int) -> ConsumerInfo:
    return ConsumerInfo(
        name=name,
        stream_name="TELEMETRY",
        config=ConsumerConfig(durable_name=name),
        created=None,  # type: ignore[arg-type]
        num_pending=pending,
        num_ack_pending=ack_pending,
        ack_floor=SequenceInfo(consumer_seq=ack_floor, stream_seq=0),
    )


def controller(js: Any, **settings: Any) -> AdmissionController:
    ingest = IngestSettings(admission_high=100, admission_low=10, **settings)
    return AdmissionController(js, settings=ingest, partitions=2, clock=ManualClock())


async def test_controller_sums_the_engine_consumers_only() -> None:
    js = MagicMock()

    async def consumers_info(stream: str, offset: int | None = None) -> list[ConsumerInfo]:
        return [
            consumer("engine-p0", pending=30, ack_pending=20, ack_floor=7),
            consumer("engine-p1", pending=40, ack_pending=15, ack_floor=9),
            consumer("trail-reader", pending=10_000, ack_pending=0, ack_floor=0),
        ]

    js.consumers_info = consumers_info
    admissions = controller(js)
    backlog = await admissions.sample()
    assert backlog is not None
    assert backlog.pending == 105
    assert dict(backlog.acked) == {"engine-p0": 7, "engine-p1": 9}
    await admissions.sample_once()
    assert not admissions.admitting  # 105 >= high
    assert admissions.snapshot()["admission"] == "shedding"
    assert admissions.snapshot()["lag"] == 105


async def test_controller_fails_closed_on_errors_and_missing_consumers() -> None:
    js = MagicMock()
    calls = 0

    async def consumers_info(stream: str, offset: int | None = None) -> list[ConsumerInfo]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [consumer("engine-p0", pending=0, ack_pending=0, ack_floor=0)]
        raise TimeoutError

    js.consumers_info = consumers_info
    admissions = controller(js)
    assert await admissions.sample() is None  # engine-p1 is missing
    assert await admissions.sample() is None  # broker did not answer
    await admissions.sample_once()
    assert not admissions.admitting
    assert admissions.snapshot() == {"admission": "shedding", "lag": None, "drain_rate": 0.0}


async def test_controller_reads_every_page_of_the_consumer_listing() -> None:
    js = MagicMock()
    offsets: list[int | None] = []
    filler = [consumer(f"x{i}", pending=0, ack_pending=0, ack_floor=0) for i in range(256)]

    async def consumers_info(stream: str, offset: int | None = None) -> list[ConsumerInfo]:
        offsets.append(offset)
        if not offset:
            return filler
        return [
            consumer("engine-p0", pending=1, ack_pending=0, ack_floor=0),
            consumer("engine-p1", pending=2, ack_pending=0, ack_floor=0),
        ]

    js.consumers_info = consumers_info
    backlog = await controller(js).sample()
    assert backlog is not None
    assert backlog.pending == 3
    assert offsets == [0, 256]


async def test_run_samples_until_stopped_and_survives_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    js = MagicMock()
    calls = 0

    async def consumers_info(stream: str, offset: int | None = None) -> list[ConsumerInfo]:
        nonlocal calls
        calls += 1
        return [
            consumer("engine-p0", pending=0, ack_pending=0, ack_floor=calls),
            consumer("engine-p1", pending=0, ack_pending=0, ack_floor=calls),
        ]

    js.consumers_info = consumers_info
    admissions = controller(js, admission_sample_ms=50)
    original = admissions.admission.observe
    crashed = False

    def flaky(backlog: Backlog | None) -> bool:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("boom")
        return original(backlog)

    monkeypatch.setattr(admissions.admission, "observe", flaky)
    stop = asyncio.Event()
    task = asyncio.create_task(admissions.run(stop))
    await eventually(lambda: calls >= 3)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert admissions.admitting
