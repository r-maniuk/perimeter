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
from perimeter.api.bodylimit import BodyLimitMiddleware
from perimeter.api.ingest.admission import AdmissionController
from perimeter.api.ingest.publisher import TelemetryPublisher
from perimeter.api.live.hub import LiveHub
from perimeter.api.live.ops import OpsBoard
from perimeter.api.live.sessions import SessionRegistry
from perimeter.api.ratelimit import RateLimiter
from perimeter.api.routes import (
    alerts,
    devices,
    geozones,
    health,
    live,
    session,
    sessions,
    telemetry,
)
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
    snapshot: dict[str, Any] = {
        "loop_lag_p99_ms": round(state.looplag.percentile(0.99) * 1000, 2),
        "db_pool_checked_out": state.db.pool.checkedout(),  # type: ignore[attr-defined]
        **state.admission.snapshot(),
        **state.publisher.snapshot(),
    }
    # --- live ---
    snapshot |= state.registry.snapshot() | state.hub.snapshot() | state.ops.snapshot()
    return snapshot


async def _start_live(state: AppState) -> None:
    """Session registry, ops board and hub; sockets are served once this returns."""
    state.registry = SessionRegistry(
        state.js,
        state.nc,
        instance=state.instance,
        max_per_user=state.settings.live.max_sessions_per_user,
    )
    await state.registry.start()
    state.ops = OpsBoard(state.nc)
    await state.ops.start()
    state.hub = LiveHub(
        state.settings,
        nc=state.nc,
        js=state.js,
        db=state.db,
        registry=state.registry,
        ops=state.ops,
        revoked=state.revoked,
        instance=state.instance,
    )
    await state.hub.start()
    state.spawn(state.registry.run(state.stop), "live-registry")
    state.spawn(state.ops.run(state.stop), "live-ops")
    state.spawn(state.hub.run(state.stop), "live-hub")


async def _stop_live(state: AppState) -> None:
    """Close every live socket (1001) while the broker connection is still up."""
    for component in ("hub", "ops", "registry"):
        if not hasattr(state, component):  # the start failed before creating it
            continue
        try:
            await getattr(state, component).close()
        except Exception:
            log.exception("api.live_stop_failed", component=component)


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
        # --- ingest ---
        admission = AdmissionController(
            js, settings=settings.ingest, partitions=settings.telemetry.partitions
        )
        await admission.sample_once()  # the first request already sees a measured backlog
        publisher = TelemetryPublisher(
            nc, settings=settings.ingest, ack_timeout_s=settings.nats.request_timeout_s
        )
        # --- end ingest ---
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
            admission=admission,
            publisher=publisher,
        )
        app.state.perimeter = state
        state.spawn(state.looplag.run(state.stop), "looplag")
        # --- ingest ---
        state.spawn(admission.run(state.stop), "admission")
        # --- end ingest ---
        # --- live ---
        await _start_live(state)
        heartbeat = Heartbeat(
            nc, service="api", instance=instance, snapshot=lambda: _snapshot(state)
        )
        state.spawn(heartbeat.run(state.stop), "heartbeat")
        log.info("api.started", instance=instance, version=__version__)
        yield
    finally:
        if state is not None:
            # --- live ---
            await _stop_live(state)
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
    app.state.login_limiter = RateLimiter(rate_per_minute=settings.security.login_rate_per_minute)
    errors.install(app)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.ingest.max_body_bytes)
    app.include_router(health.router)
    v1 = APIRouter(prefix="/v1")
    for router in (
        session.router,
        geozones.router,
        alerts.router,
        devices.router,
        telemetry.router,
        # --- live ---
        live.router,
        sessions.router,
    ):
        v1.include_router(router)
    app.include_router(v1)  # after every router is on v1: inclusion copies the routes
    app.mount("/metrics", make_asgi_app())
    return app
