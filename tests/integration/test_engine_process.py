"""``python -m perimeter.engine`` as a real process: start-up, metrics, signals, exit codes."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from nats.js import JetStreamContext
from nats.js.errors import NoKeysError
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.config import Settings
from perimeter.wire import subjects

INSTANCE = "engine-proc"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


class EngineProcess:
    """The engine started exactly as its container does, with JSON logs on stdout."""

    def __init__(self, process: asyncio.subprocess.Process, metrics_port: int) -> None:
        self.process = process
        self.metrics_port = metrics_port
        self.events: list[dict[str, Any]] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process.stdout is not None
        async for line in self.process.stdout:
            try:
                self.events.append(json.loads(line))
            except ValueError:
                self.events.append({"event": "raw", "line": line.decode(errors="replace")})

    def seen(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == event]

    async def wait_for(self, predicate: Callable[[], bool], within: float = 15.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while not predicate():
            if loop.time() > deadline or self.process.returncode is not None:
                lines = "\n".join(json.dumps(e) for e in self.events[-30:])
                msg = f"engine process did not get there in {within}s; last log lines:\n{lines}"
                raise AssertionError(msg)
            await asyncio.sleep(0.05)

    async def exit_code(self, within: float = 15.0) -> int:
        code = await asyncio.wait_for(self.process.wait(), within)
        await asyncio.wait_for(self._reader, 5)
        return code


Launch = Callable[..., Awaitable[EngineProcess]]


def environment(settings: Settings, metrics_port: int, **extra: str) -> dict[str, str]:
    database = settings.database
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "DATABASE_HOST": database.host,
        "DATABASE_PORT": str(database.port),
        "DATABASE_NAME": database.name,
        "DATABASE_USER": database.user,
        "DATABASE_PASSWORD": database.password.get_secret_value(),
        "NATS_URL": settings.nats.url,
        "TELEMETRY_PARTITIONS": str(settings.telemetry.partitions),
        "ENGINE_LEASE_TTL_S": str(settings.engine.lease_ttl_s),
        "ENGINE_FETCH_WAIT_S": "0.2",
        "ENGINE_METRICS_PORT": str(metrics_port),
        "ENGINE_INSTANCE_ID": INSTANCE,
        "LOG_LEVEL": "INFO",
        **extra,
    }


@pytest.fixture
async def launch(
    settings: Settings, provisioned: topology.Topology, db: AsyncEngine
) -> AsyncIterator[Launch]:
    started: list[EngineProcess] = []

    async def start(**extra: str) -> EngineProcess:
        port = free_port()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "perimeter.engine",
            env=environment(settings, port, **extra),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        engine = EngineProcess(process, port)
        started.append(engine)
        return engine

    yield start
    for engine in started:
        if engine.process.returncode is None:
            engine.process.kill()
            await engine.process.wait()


async def probe(url: str) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "perimeter.tools.probe", url
    )
    return await asyncio.wait_for(process.wait(), 10)


async def lease_keys(js: JetStreamContext) -> list[str]:
    kv = await js.key_value(subjects.KV_ENGINE)
    try:
        return sorted(await kv.keys())
    except NoKeysError:
        return []


async def test_the_process_serves_metrics_and_hands_everything_back_on_sigterm(
    launch: Launch, js: JetStreamContext
) -> None:
    engine = await launch()
    await engine.wait_for(lambda: len(engine.seen("engine.partition_acquired")) == 4)
    assert engine.seen("engine.started")[0]["instance"] == INSTANCE
    metrics = f"http://127.0.0.1:{engine.metrics_port}/metrics"
    assert await probe(metrics) == 0  # exactly the container health check
    assert await lease_keys(js) == ["m.engine-proc", "p.0", "p.1", "p.2", "p.3"]

    engine.process.send_signal(signal.SIGTERM)
    assert await engine.exit_code() == 0
    assert engine.seen("engine.stopped")
    assert engine.seen("raw") == [], "every line of the output should be a JSON log record"
    assert len(engine.seen("engine.partition_released")) == 4
    assert await lease_keys(js) == [], "leases and membership should be given back"
    assert await probe(metrics) == 1


async def test_a_second_signal_cuts_the_shutdown_short(
    launch: Launch, js: JetStreamContext
) -> None:
    engine = await launch(ENGINE_FETCH_WAIT_S="5")  # a graceful stop now takes seconds
    await engine.wait_for(lambda: len(engine.seen("engine.partition_acquired")) == 4)
    await asyncio.sleep(0.3)  # let every worker enter its long fetch
    engine.process.send_signal(signal.SIGTERM)
    await engine.wait_for(lambda: bool(engine.seen("engine.stopping")))
    engine.process.send_signal(signal.SIGINT)
    assert await engine.exit_code(within=5) == 0
    assert engine.seen("engine.shutdown_forced")
    assert "p.0" in await lease_keys(js), "an aborted engine leaves its leases to expire"


async def test_the_process_exits_non_zero_when_it_cannot_run(
    launch: Launch, settings: Settings
) -> None:
    wrong = str(settings.telemetry.partitions * 2)
    engine = await launch(TELEMETRY_PARTITIONS=wrong)
    assert await engine.exit_code() == 1
    (refusal,) = engine.seen("engine.cannot_start")
    assert "partitions" in refusal["error"]
