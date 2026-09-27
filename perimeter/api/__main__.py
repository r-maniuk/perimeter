"""Run one API replica: ``python -m perimeter.api``."""

from __future__ import annotations

import os

import uvicorn

from perimeter.config import load_settings
from perimeter.ops.logging import configure_logging


def main() -> None:
    settings = load_settings()
    configure_logging(
        service="api",
        level=settings.observability.log_level,
        json=not settings.is_development,
    )
    uvicorn.run(
        "perimeter.api.app:create_app",
        factory=True,
        # Inside the container network only the edge proxy can reach this port.
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("PORT", "8000")),
        loop="uvloop",
        http="httptools",
        ws="websockets-sansio",
        # Position frames are forwarded pre-encoded: no per-socket compression.
        ws_per_message_deflate=False,
        ws_max_size=1024 * 1024,
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        # Longer than the edge keeps an idle upstream connection (90 s), so the edge always closes
        # it first; otherwise a request could meet a connection just being closed here, and the
        # edge does not retry a POST.
        timeout_keep_alive=120,
        proxy_headers=True,
        forwarded_allow_ips="*",  # only the edge proxy can reach this port (internal network)
        access_log=False,
        log_config=None,
        server_header=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
