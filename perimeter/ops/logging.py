"""Structured logging: JSON lines in production, readable console output in development.

Standard-library loggers (uvicorn, alembic, nats) are routed through the same processors so every
line of a container's output has the same shape. Credentials passed in URLs (``?token=`` of
command-line WebSocket clients, which uvicorn logs with the request path) are redacted on the way.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import msgspec
import structlog

_json = msgspec.json.Encoder(enc_hook=str)
_CREDENTIAL_IN_URL = re.compile(r"([?&](?:token|access_token)=)[^&#\s\"']+")


def _serialize(event: Any, **_: Any) -> str:
    return _json.encode(event).decode()


def redact_credentials(
    _: Any, __: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Replace the value of ``token=`` / ``access_token=`` query parameters in the message."""
    event = event_dict.get("event")
    if isinstance(event, str) and "token=" in event:
        event_dict["event"] = _CREDENTIAL_IN_URL.sub(r"\1[redacted]", event)
    return event_dict


def configure_logging(*, service: str, level: str = "INFO", json: bool = True) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_credentials,
    ]
    renderer: structlog.types.Processor
    if json:
        renderer = structlog.processors.JSONRenderer(serializer=_serialize)
        shared.append(structlog.processors.dict_tracebacks)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)
