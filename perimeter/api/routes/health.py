"""Liveness and readiness.

``/healthz`` answers as long as the event loop runs (container restart signal). ``/readyz`` checks
what this replica needs to serve traffic — database, broker, a responsive loop — for the compose
health check and any orchestrator that routes by readiness. The edge proxy does not probe it: it
takes a replica out of rotation only when connections to it fail (infra/caddy/Caddyfile).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from perimeter.api.deps import State

router = APIRouter(tags=["health"])

READY_MAX_LOOP_LAG_S = 0.5


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(state: State) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with asyncio.timeout(1.0), state.db.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # any failure means "not ready", with the reason
        checks["database"] = f"error: {type(exc).__name__}"
    checks["broker"] = "ok" if state.nc.is_connected else "disconnected"
    lag = state.looplag.percentile(0.99)
    checks["event_loop"] = "ok" if lag < READY_MAX_LOOP_LAG_S else f"lagging {lag:.3f}s"
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "degraded", "checks": checks},
        status_code=200 if ready else 503,
    )
