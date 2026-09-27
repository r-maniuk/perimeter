from __future__ import annotations

import asyncio
import json
from typing import cast

from nats.aio.client import Client as NatsClient

from perimeter.api.live.ops import OpsBoard
from tests.support import eventually


class Viewer:
    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []

    def offer_text(self, frame: bytes) -> bool:
        self.frames.append(json.loads(frame))
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def board(clock: Clock | None = None, interval_s: float = 1.0) -> OpsBoard:
    return OpsBoard(
        cast("NatsClient", None),
        interval_s=interval_s,
        clock=clock or Clock(),
        wall_clock=lambda: 1_790_000_000.25,
    )


def heartbeat(service: str, instance: str, **values: object) -> bytes:
    return json.dumps({"service": service, "instance": instance, **values}).encode()


def test_the_latest_heartbeat_of_each_instance_is_kept_and_forwarded_verbatim() -> None:
    ops = board()
    ops.record("sys.metrics.engine.e1", heartbeat("engine", "e1", reports_rate=10))
    ops.record("sys.metrics.api.a1", heartbeat("api", "a1", sessions=2))
    ops.record("sys.metrics.engine.e1", heartbeat("engine", "e1", reports_rate=12))
    frame = json.loads(ops.frame())
    assert frame["type"] == "ops"
    assert frame["ts"] == 1_790_000_000.25
    assert frame["services"] == [
        {"service": "api", "instance": "a1", "sessions": 2},
        {"service": "engine", "instance": "e1", "reports_rate": 12},
    ]


def test_heartbeats_that_are_not_json_objects_are_ignored() -> None:
    ops = board()
    ops.record("sys.metrics.engine.e1", b"not json")
    ops.record("sys.metrics.engine.e2", b"[1,2]")
    assert json.loads(ops.frame())["services"] == []


def test_instances_silent_for_five_seconds_disappear() -> None:
    clock = Clock()
    ops = board(clock)
    ops.record("sys.metrics.engine.e1", heartbeat("engine", "e1"))
    clock.now += 3
    ops.record("sys.metrics.engine.e2", heartbeat("engine", "e2"))
    clock.now += 2.5
    services = json.loads(ops.frame())["services"]
    assert [s["instance"] for s in services] == ["e2"]


async def test_viewers_get_a_frame_at_once_and_then_every_interval() -> None:
    ops = board(interval_s=0.02)
    ops.record("sys.metrics.api.a1", heartbeat("api", "a1"))
    viewer, other = Viewer(), Viewer()
    ops.watch(viewer)
    ops.watch(viewer)
    assert len(viewer.frames) == 1
    assert ops.snapshot() == {"ops_viewers": 1}
    stop = asyncio.Event()
    task = asyncio.create_task(ops.run(stop))
    await eventually(lambda: len(viewer.frames) >= 3)
    ops.unwatch(viewer)
    seen = len(viewer.frames)
    await asyncio.sleep(0.06)
    assert len(viewer.frames) <= seen + 1
    assert other.frames == []
    stop.set()
    await asyncio.wait_for(task, 1)
    await ops.close()
