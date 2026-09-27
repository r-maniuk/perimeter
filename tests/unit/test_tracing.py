"""The trace context kept for later: taken from the current span, and nothing without tracing."""

from __future__ import annotations

import pytest

from perimeter.ops import tracing

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


def test_without_tracing_no_context_is_kept() -> None:
    with tracing.span("request", headers={"traceparent": TRACEPARENT}):
        assert tracing.context() is None


def test_the_current_context_is_kept_as_message_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_enabled", True)  # spans are made, and exported nowhere
    assert tracing.context() is None  # outside any span there is nothing to keep
    with tracing.span("request", headers={"traceparent": TRACEPARENT}):
        kept = tracing.context()
    assert kept is not None
    assert kept["traceparent"].split("-")[1] == TRACE_ID
