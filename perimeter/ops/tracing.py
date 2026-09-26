"""Optional OpenTelemetry tracing.

Enabled only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (the ``observability`` compose profile
points it at Jaeger). Trace context crosses NATS in the standard ``traceparent`` header, so one
trace follows a report from the ingest request through the engine batch to the live push.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

import structlog

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
