"""Trail reads through batched direct gets: bounds, junk in the log, failures, broker rights."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator

import nats
import nats.errors
import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig

from perimeter.bus import topology
from perimeter.bus.trail import read_trail
from perimeter.domain.reports import TelemetryRecord
from perimeter.wire import subjects, telemetry
from tests.integration.conftest import NATS_IMAGE

MINUTE_MS = 60_000


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def report(js: JetStreamContext, device: str, recorded_at_ms: int, lon: float = 4.9) -> None:
    record = TelemetryRecord(device, recorded_at_ms, now_ms(), 52.37, lon)
    await js.publish(
        subjects.telemetry(device),
        telemetry.encode(record),
        headers={"Nats-Msg-Id": telemetry.dedup_id(record)},
    )


async def test_a_trail_is_read_in_batches_until_caught_up(
    nc: NatsClient, js: JetStreamContext, provisioned: object
) -> None:
    now = now_ms()
    for i in range(10):
        await report(js, "walker", now - 10_000 + i * 1_000, lon=4.0 + i / 100)
    await report(js, "other", now - 5_000)
    trail = await read_trail(
        nc,
        "walker",
        since_ms=now - MINUTE_MS,
        now_ms=now,
        max_points=100,
        timeout_s=2,
        batch_size=3,
    )
    assert [p.lon for p in trail.points] == [4.0 + i / 100 for i in range(10)]
    assert trail.complete


async def test_an_unknown_device_has_an_empty_complete_trail(
    nc: NatsClient, provisioned: object
) -> None:
    now = now_ms()
    trail = await read_trail(
        nc, "nobody", since_ms=now - MINUTE_MS, now_ms=now, max_points=10, timeout_s=2
    )
    assert trail.points == []
    assert trail.complete


async def test_a_dense_window_returns_the_newest_points(
    nc: NatsClient, js: JetStreamContext, provisioned: object
) -> None:
    now = now_ms()
    for minute in range(30):  # one report a minute for half an hour
        await report(js, "busy", now - minute * MINUTE_MS - 1_000)
    trail = await read_trail(
        nc, "busy", since_ms=now - 30 * MINUTE_MS, now_ms=now, max_points=10, timeout_s=2
    )
    newest = [p.recorded_at_ms for p in trail.points]
    assert 1 <= len(newest) <= 10
    assert newest == sorted(newest)
    assert newest[-1] == now - 1_000  # the most recent part of the track, not the oldest
    assert not trail.complete


async def test_a_burst_is_cut_by_the_scan_budget_and_marked_incomplete(
    nc: NatsClient, js: JetStreamContext, provisioned: object
) -> None:
    now = now_ms()
    for i in range(30):  # stored within a second: a later start cannot thin them out
        await report(js, "burst", now - 50_000 + i * 1_000)
    trail = await read_trail(
        nc, "burst", since_ms=now - 30 * MINUTE_MS, now_ms=now, max_points=10, timeout_s=2
    )
    # 20 messages scanned (twice the point cap), the newest 10 of them kept
    assert [p.recorded_at_ms for p in trail.points] == [
        now - 50_000 + i * 1_000 for i in range(10, 20)
    ]
    assert not trail.complete


async def test_undecodable_messages_are_skipped(
    nc: NatsClient, js: JetStreamContext, provisioned: object
) -> None:
    now = now_ms()
    await report(js, "noisy", now - 3_000, lon=4.1)
    await js.publish(subjects.telemetry("noisy"), b"\xc1 not msgpack")
    await report(js, "noisy", now - 1_000, lon=4.2)
    trail = await read_trail(
        nc, "noisy", since_ms=now - MINUTE_MS, now_ms=now, max_points=100, timeout_s=2
    )
    assert [p.lon for p in trail.points] == [4.1, 4.2]
    assert trail.complete


async def test_the_deadline_bounds_the_read(
    nc: NatsClient, js: JetStreamContext, provisioned: object
) -> None:
    now = now_ms()
    await report(js, "slow", now - 1_000)
    trail = await read_trail(
        nc, "slow", since_ms=now - MINUTE_MS, now_ms=now, max_points=100, timeout_s=1e-6
    )
    assert trail.points == []
    assert not trail.complete


async def test_a_missing_stream_is_an_incomplete_trail_not_an_error(
    nc: NatsClient, js: JetStreamContext, provisioned: object
) -> None:
    await js.delete_stream(subjects.TELEMETRY_STREAM)
    now = now_ms()
    trail = await read_trail(
        nc, "veh-1", since_ms=now - MINUTE_MS, now_ms=now, max_points=10, timeout_s=2
    )
    assert trail.points == []
    assert not trail.complete


# The api's broker account may read TELEMETRY only through direct gets: it may not create, pull
# from or delete consumers there. Prove the trail needs nothing more, on a real server.
RESTRICTED_CONFIG = """
jetstream {}
authorization {
  users = [
    {user: admin, password: admin}
    {user: api, password: api, permissions: {
      publish: {
        allow: ["tlm.*", "$JS.API.DIRECT.GET.TELEMETRY"]
        deny: ["$JS.API.CONSUMER.>"]
      }
      subscribe: {allow: ["_INBOX.api.>"]}
    }}
  ]
}
"""


@pytest.fixture(scope="module")
def restricted_nats() -> Iterator[str]:
    from testcontainers.core.container import DockerContainer  # noqa: PLC0415
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy  # noqa: PLC0415

    container = (
        DockerContainer(NATS_IMAGE)
        .with_env("NATS_CONF", RESTRICTED_CONFIG)
        .with_command(
            [
                "sh",
                "-c",
                'printf "%s" "$NATS_CONF" > /tmp/nats.conf && exec nats-server -c /tmp/nats.conf',
            ]
        )
        .with_exposed_ports(4222)
        .waiting_for(LogMessageWaitStrategy("Server is ready"))
    )
    with container:
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@pytest.fixture
async def api_account(restricted_nats: str) -> AsyncIterator[NatsClient]:
    admin = await nats.connect(restricted_nats, user="admin", password="admin")
    await topology.ensure(admin.jetstream(), topology.Topology(partitions=4))
    client = await nats.connect(
        restricted_nats, user="api", password="api", inbox_prefix=b"_INBOX.api"
    )
    yield client
    await client.close()
    for stream in await admin.jetstream().streams_info():
        if stream.config.name:
            await admin.jetstream().delete_stream(stream.config.name)
    await admin.close()


async def test_trails_need_only_direct_get_rights(api_account: NatsClient) -> None:
    js = api_account.jetstream()
    now = now_ms()
    for i in range(3):
        await report(js, "veh-7", now - 3_000 + i * 1_000, lon=4.5 + i / 10)
    trail = await read_trail(
        api_account, "veh-7", since_ms=now - MINUTE_MS, now_ms=now, max_points=10, timeout_s=2
    )
    assert [p.lon for p in trail.points] == [4.5, 4.6, 4.7]
    assert trail.complete
    with pytest.raises(nats.errors.TimeoutError):  # the broker drops a consumer request from api
        await js.add_consumer(
            subjects.TELEMETRY_STREAM, ConsumerConfig(filter_subject="tlm.*.veh-7"), timeout=0.5
        )
