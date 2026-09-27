"""Optional OpenTelemetry tracing.

Enabled only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (the ``observability`` compose profile
points it at Jaeger). Trace context crosses NATS in the standard ``traceparent`` header: from the
ingest request into the engine batch that applies the report, from that batch's transaction into
the alert events it writes, and from those to the API replicas that deliver them to sockets.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.config import ObservabilitySettings

log = structlog.get_logger(__name__)

try:  # the "tracing" extra is optional
    from opentelemetry import context as otel_context
    from opentelemetry import propagate, trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    _AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _AVAILABLE = False

_enabled = False


def configure(settings: ObservabilitySettings, *, service: str) -> bool:
    global _enabled  # noqa: PLW0603 - process-wide switch, set once at start-up
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint or not _AVAILABLE:
        return False
    provider = TracerProvider(
        resource=Resource.create({"service.name": f"perimeter-{service}"}),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _enabled = True
    log.info("tracing.enabled", endpoint=endpoint, sample_ratio=settings.otel_sample_ratio)
    return True


def enabled() -> bool:
    return _enabled


def instrument_app(app: FastAPI) -> None:
    """Server spans for every request (health and metrics probes excluded)."""
    if _enabled:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")


def instrument_database(engine: AsyncEngine) -> None:
    """Client spans for every SQL statement of this engine.

    The instrumentation hooks SQLAlchemy's cursor events, which 2.1 keeps unchanged; its package
    metadata simply predates 2.1, so the version gate is skipped deliberately (the spans are
    checked end to end in the observability profile).
    """
    if _enabled:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, skip_dep_check=True)


def inject(headers: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Add ``traceparent`` (and friends) for the current span to outgoing message headers."""
    if _enabled:
        propagate.inject(headers)
    return headers


@contextmanager
def _span_from(headers: Mapping[str, str] | None, name: str, **attributes: Any) -> Iterator[None]:
    parent = propagate.extract(dict(headers or {}))
    token = otel_context.attach(parent)
    try:
        with trace.get_tracer("perimeter").start_as_current_span(name, attributes=attributes):
            yield
    finally:
        otel_context.detach(token)


def span(
    name: str, *, headers: Mapping[str, str] | None = None, **attributes: Any
) -> AbstractContextManager[None]:
    """A span continuing the trace carried by ``headers`` (no-op when tracing is off)."""
    if not _enabled:
        return nullcontext()
    return _span_from(headers, name, **attributes)
