"""Request body size limit for every HTTP endpoint.

The ASGI server does not bound request bodies, and FastAPI reads a JSON body completely before any
endpoint code runs, even on unauthenticated routes such as sign-in. Without a limit, one request
could make a replica buffer gigabytes. This middleware refuses a declared ``Content-Length`` above
the limit before the application sees the request, and counts the bytes of bodies sent without
one (chunked) as the application reads them, failing the read with 413 as soon as the limit is
crossed — nothing beyond the limit is ever buffered.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from perimeter.api.errors import problem_response


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self._max_bytes:
            response = problem_response(413, "payload_too_large", self._detail())
            await response(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise HTTPException(413, self._detail())
            return message

        await self._app(scope, limited, send)

    def _detail(self) -> str:
        return f"request bodies are limited to {self._max_bytes} bytes"


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
