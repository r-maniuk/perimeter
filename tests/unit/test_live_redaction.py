"""Session tokens given to ``/v1/live?token=`` must never reach the logs."""

import logging

import pytest
import structlog

from perimeter.ops.logging import configure_logging, redact_credentials

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            f'10.0.0.2:5123 - "WebSocket /v1/live?token={TOKEN}&resume_after=41" [accepted]',
            '10.0.0.2:5123 - "WebSocket /v1/live?token=[redacted]&resume_after=41" [accepted]',
        ),
        (
            f"GET /v1/live?resume_after=3&token={TOKEN}",
            "GET /v1/live?resume_after=3&token=[redacted]",
        ),
        (f"/x?access_token={TOKEN}#frag", "/x?access_token=[redacted]#frag"),
        (
            "ingest_token=abc is a setting name, not a query",
            "ingest_token=abc is a setting name, not a query",
        ),
        ("nothing to hide", "nothing to hide"),
    ],
)
def test_credentials_in_urls_are_redacted(message: str, expected: str) -> None:
    assert redact_credentials(None, "info", {"event": message})["event"] == expected


def test_uvicorn_websocket_lines_are_redacted_on_the_way_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging(service="api", json=True)
        logging.getLogger("uvicorn.error").info(
            '%s - "WebSocket %s" [accepted]',
            "10.0.0.2:5123",
            f"/v1/live?token={TOKEN}&resume_after=41",
        )
        output = capsys.readouterr().out
    finally:
        root.handlers, root.level = saved
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
    assert TOKEN not in output
    assert "/v1/live?token=[redacted]&resume_after=41" in output
