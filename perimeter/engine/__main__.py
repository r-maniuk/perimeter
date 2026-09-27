"""Run one geofence engine instance: ``python -m perimeter.engine``."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Coroutine
from contextlib import suppress
from types import FrameType
from typing import Any

import structlog
import uvloop
from prometheus_client import start_http_server

from perimeter.bus.topology import TopologyError
from perimeter.config import Settings, load_settings
from perimeter.engine.service import EngineService, StartupError
from perimeter.ops import tracing
from perimeter.ops.logging import configure_logging

log = structlog.get_logger(__name__)

HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def serve(settings: Settings) -> None:
    """Run until SIGTERM or SIGINT; a second signal skips the graceful part."""
    tracing.configure(settings.observability, service="engine")
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    abort = asyncio.Event()

    def escalate() -> None:
        (abort if stop.is_set() else stop).set()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(escalate)

    # signal.signal rather than loop.add_signal_handler, as uvicorn does for the api: the
    # latter goes through a coroutine check that Python 3.14 deprecates under uvloop.
    previous = {signum: signal.signal(signum, on_signal) for signum in HANDLED_SIGNALS}
    # Reachable only on the internal network: Prometheus and the container health check.
    metrics, _ = start_http_server(settings.engine.metrics_port, addr="0.0.0.0")  # noqa: S104
    service = EngineService(settings)
    try:
        if not await _first(service.start(), stop):
            return  # stopped while still starting (e.g. waiting for the database)
        await stop.wait()
        log.info("engine.stopping", instance=service.instance)
        if not await _first(service.stop(), abort):
            log.warning("engine.shutdown_forced", instance=service.instance)
            await service.abort()
    finally:
        await asyncio.to_thread(metrics.shutdown)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


async def _first(work: Coroutine[Any, Any, None], event: asyncio.Event) -> bool:
    """Run ``work`` unless ``event`` fires first (then cancel it); ``True`` if ``work`` finished."""
    task = asyncio.create_task(work)
    interrupt = asyncio.create_task(event.wait())
    try:
        await asyncio.wait({task, interrupt}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        interrupt.cancel()
    if task.done():
        task.result()
        return True
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return False


def main() -> None:
    settings = load_settings()
    configure_logging(
        service="engine",
        level=settings.observability.log_level,
        json=not settings.is_development,
    )
    try:
        uvloop.run(serve(settings))
    except (TopologyError, StartupError) as exc:
        log.error("engine.cannot_start", error=str(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
