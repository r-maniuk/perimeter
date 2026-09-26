"""FastAPI application factory and lifespan of one API replica."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import structlog
from fastapi import APIRouter, FastAPI
from prometheus_client import make_asgi_app

from perimeter import __version__
from perimeter.api import errors
from perimeter.api.routes import health
from perimeter.api.security import RevocationList, TokenService
from perimeter.api.state import AppState
from perimeter.bus import topology
from perimeter.bus.connection import connect
from perimeter.bus.relay import OutboxRelay
from perimeter.config import Settings, load_settings
from perimeter.ops import tracing
from perimeter.ops.heartbeat import Heartbeat, instance_id
from perimeter.ops.looplag import LoopLagMonitor
from perimeter.storage.engine import create_engine, session_factory

log = structlog.get_logger(__name__)

SHUTDOWN_GRACE_S = 5.0


def _snapshot(state: AppState) -> dict[str, Any]:
    return {
        "loop_lag_p99_ms": round(state.looplag.percentile(0.99) * 1000, 2),
        "db_pool_checked_out": state.db.pool.checkedout(),  # type: ignore[attr-defined]
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    instance = instance_id()
    tracing.configure(settings.observability, service="api")
    db = create_engine(settings.database, application_name=f"perimeter-api/{instance}")
    nc = await connect(settings.nats, name=f"perimeter-api/{instance}")
    js = nc.jetstream()
    state: AppState | None = None
    try:
        await topology.verify(js, topology.Topology.from_settings(settings))
        state = AppState(
            settings=settings,
            instance=instance,
            db=db,
            sessions=session_factory(db),
            nc=nc,
            js=js,
            relay=OutboxRelay(db, js),
            tokens=TokenService(settings.security),
            revoked=await RevocationList.open(js),
            looplag=LoopLagMonitor(),
        )
        app.state.perimeter = state
        state.spawn(state.looplag.run(state.stop), "looplag")
        heartbeat = Heartbeat(
            nc, service="api", instance=instance, snapshot=lambda: _snapshot(state)
        )
        state.spawn(heartbeat.run(state.stop), "heartbeat")
        log.info("api.started", instance=instance, version=__version__)
        yield
    finally:
        if state is not None:
            state.stop.set()
            for task in state.tasks:
                task.cancel()
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*state.tasks, return_exceptions=True), SHUTDOWN_GRACE_S
                )
            await state.revoked.close()
        with suppress(Exception):
            await nc.drain()
        await db.dispose()
        log.info("api.stopped", instance=instance)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="Perimeter",
        version=__version__,
        summary="Live device tracking and circular geofence alerts.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    errors.install(app)
    app.include_router(health.router)
    v1 = APIRouter(prefix="/v1")
    app.include_router(v1)
    app.mount("/metrics", make_asgi_app())
    return app
