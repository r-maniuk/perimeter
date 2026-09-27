from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Literal

import pytest
from nats.errors import ConnectionClosedError

from perimeter.api.ingest.publisher import (
    InflightBudget,
    IngestOverloaded,
    IngestUnavailable,
    LatencyWindow,
    RateCounter,
    TelemetryPublisher,
)
from perimeter.bus.publish import Ack, PublishError
from perimeter.config import IngestSettings
from perimeter.domain.clock import ManualClock
from perimeter.domain.reports import TelemetryRecord
from perimeter.wire import telemetry

Mode = Literal["ack", "duplicate", "fail_second", "silent", "unsendable_second"]


class FakeStream:
    """Stands in for :class:`perimeter.bus.publish.StreamPublisher`."""

    def __init__(self, mode: Mode = "ack") -> None:
        self.mode = mode
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.futures: list[asyncio.Future[Ack]] = []

    async def publish(
        self, subject: str, payload: bytes, headers: Mapping[str, str] | None = None
    ) -> asyncio.Future[Ack]:
        if self.mode == "unsendable_second" and len(self.published) == 1:
            raise ConnectionClosedError
        future: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self.published.append((subject, payload, dict(headers or {})))
        self.futures.append(future)
        index = len(self.futures)
        if self.mode in {"ack", "duplicate"}:
            future.set_result(Ack("TELEMETRY", index, duplicate=self.mode == "duplicate"))
        elif self.mode == "fail_second" and index == 2:
            future.set_exception(PublishError("no stream listens on 'tlm.veh-1'"))
        return future


def records(count: int) -> list[TelemetryRecord]:
    return [
        TelemetryRecord(f"veh-{i}", 1_790_000_000_000 + i, 1_790_000_000_500, 52.0, 4.0)
        for i in range(count)
    ]


def publisher(
    mode: Mode = "ack", *, max_inflight: int = 100, ack_timeout_s: float = 0.2
) -> tuple[TelemetryPublisher, FakeStream]:
    stream = FakeStream(mode)
    settings = IngestSettings(max_inflight=max_inflight)
    publisher_ = TelemetryPublisher(
        stream,  # type: ignore[arg-type]
        settings=settings,
        ack_timeout_s=ack_timeout_s,
        budget_wait_s=0.05,
    )
    return publisher_, stream


async def test_publishes_every_record_with_its_dedup_id_and_waits_for_acks() -> None:
    publisher_, stream = publisher()
    outcome = await publisher_.publish(records(3))
    assert outcome.accepted == 3
    assert outcome.duplicates == 0
    subject, payload, headers = stream.published[1]
    assert subject == "tlm.veh-1"
    assert telemetry.decode(payload) == records(3)[1]
    assert headers["Nats-Msg-Id"] == "veh-1:1790000000001"
    assert publisher_.budget.in_use == 0


async def test_duplicates_count_as_accepted() -> None:
    publisher_, _ = publisher("duplicate")
    outcome = await publisher_.publish(records(2))
    assert (outcome.accepted, outcome.duplicates) == (2, 2)


async def test_empty_batches_publish_nothing() -> None:
    publisher_, stream = publisher()
    assert (await publisher_.publish([])).accepted == 0
    assert stream.published == []


async def test_one_failed_ack_fails_the_batch_and_frees_the_budget() -> None:
    publisher_, stream = publisher("fail_second")
    with pytest.raises(IngestUnavailable) as error:
        await publisher_.publish(records(3))
    assert isinstance(error.value.__cause__, PublishError)
    assert publisher_.budget.in_use == 0
    # the acknowledgements still outstanding were given up, not leaked
    assert all(future.done() for future in stream.futures)


async def test_a_report_that_cannot_be_sent_fails_the_batch_and_nothing_waits_on() -> None:
    publisher_, stream = publisher("unsendable_second")
    with pytest.raises(IngestUnavailable) as error:
        await publisher_.publish(records(3))
    assert isinstance(error.value.__cause__, ConnectionClosedError)
    assert all(future.cancelled() for future in stream.futures)
    assert publisher_.budget.in_use == 0


async def test_missing_acks_time_out_and_are_cancelled() -> None:
    publisher_, stream = publisher("silent", ack_timeout_s=0.05)
    with pytest.raises(IngestUnavailable):
        await publisher_.publish(records(2))
    assert all(future.cancelled() for future in stream.futures)
    assert publisher_.budget.in_use == 0


async def test_a_full_budget_refuses_quickly() -> None:
    publisher_, _ = publisher("silent", max_inflight=4, ack_timeout_s=5)
    blocker = asyncio.create_task(publisher_.publish(records(3)))
    await asyncio.sleep(0)
    assert publisher_.budget.in_use == 3
    with pytest.raises(IngestOverloaded):
        await publisher_.publish(records(2))
    blocker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocker
    assert publisher_.budget.in_use == 0


async def test_snapshot_reports_rates_and_latency() -> None:
    clock = ManualClock()
    publisher_ = TelemetryPublisher(
        FakeStream(),  # type: ignore[arg-type]
        settings=IngestSettings(),
        ack_timeout_s=1,
        clock=clock,
    )
    await publisher_.publish(records(5))
    publisher_.note_rejected(["invalid_latitude", "missing_field"])
    clock.advance(1)
    snapshot = publisher_.snapshot()
    assert snapshot["ingest_rate"] == 5
    assert snapshot["ingest_rejected_rate"] == 2
    assert snapshot["ingest_inflight"] == 0
    assert snapshot["publish_p99_ms"] is not None


async def test_budget_is_first_come_first_served() -> None:
    budget = InflightBudget(10)
    assert await budget.acquire(8, wait_s=0)
    order: list[str] = []

    async def take(name: str, amount: int) -> None:
        assert await budget.acquire(amount, wait_s=1)
        order.append(name)

    large = asyncio.create_task(take("large", 6))
    await asyncio.sleep(0)
    small = asyncio.create_task(take("small", 1))  # would fit now, but waits its turn
    await asyncio.sleep(0.01)
    assert order == []
    budget.release(8)
    await asyncio.gather(large, small)
    assert order == ["large", "small"]
    assert budget.in_use == 7


async def test_budget_wait_times_out_and_unblocks_the_queue() -> None:
    budget = InflightBudget(4)
    assert await budget.acquire(3, wait_s=0)
    assert not await budget.acquire(4, wait_s=0.01)  # gives up
    assert await budget.acquire(1, wait_s=0.01)  # not stuck behind the one that left
    assert budget.in_use == 4


async def test_budget_grant_racing_a_cancellation_is_returned() -> None:
    budget = InflightBudget(2)
    assert await budget.acquire(2, wait_s=0)
    waiter = asyncio.create_task(budget.acquire(2, wait_s=5))
    await asyncio.sleep(0)
    budget.release(2)  # grants the waiter synchronously ...
    waiter.cancel()  # ... which is cancelled before it could run
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert budget.in_use == 0


async def test_budget_rejects_impossible_requests() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        InflightBudget(0)
    with pytest.raises(ValueError, match="cannot acquire"):
        await InflightBudget(2).acquire(3, wait_s=0)


def test_rate_counter_reports_the_last_complete_second() -> None:
    clock = ManualClock()
    rate = RateCounter(clock)
    rate.add(3)
    rate.add(2)
    assert rate.rate() == 0.0  # the current second is still running
    clock.advance(1)
    assert rate.rate() == 5.0
    clock.advance(1)
    assert rate.rate() == 0.0
    clock.advance(10)
    rate.add(1)
    clock.advance(1)
    assert rate.rate() == 1.0


def test_latency_window_forgets_old_samples() -> None:
    clock = ManualClock()
    window = LatencyWindow(horizon_s=5, clock=clock)
    assert window.percentile(0.99) is None
    for value in range(1, 101):
        window.add(value / 1000)
    assert window.percentile(0.99) == pytest.approx(0.099)
    clock.advance(6)
    assert window.percentile(0.99) is None
