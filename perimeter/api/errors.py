"""Errors as RFC 9457 problem details (``application/problem+json``) with stable codes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger(__name__)

PROBLEM_TYPE_BASE = "https://perimeter.dev/problems/"
MEDIA_TYPE = "application/problem+json"


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or HTTPStatus(status).phrase
        self.headers = headers
        self.extra = extra or {}


def problem_body(
    status: int, code: str, detail: str, *, title: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "type": PROBLEM_TYPE_BASE + code,
        "title": title or HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
        **extra,
    }


def problem_response(
    status: int,
    code: str,
    detail: str,
    *,
    title: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    return JSONResponse(
        problem_body(status, code, detail, title=title, **extra),
        status_code=status,
        media_type=MEDIA_TYPE,
        headers=headers,
    )


def not_found(what: str) -> ProblemError:
    return ProblemError(404, "not_found", f"{what} does not exist")


def unauthorized(detail: str = "sign in to continue") -> ProblemError:
    return ProblemError(401, "unauthorized", detail, headers={"WWW-Authenticate": "Bearer"})


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    412: "precondition_failed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "unavailable",
}


def install(app: FastAPI) -> None:
    async def on_problem(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProblemError)
        return problem_response(
            exc.status, exc.code, exc.detail, title=exc.title, headers=exc.headers, **exc.extra
        )

    async def on_validation(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errors = [
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", ""),
                "type": error.get("type"),
            }
            for error in exc.errors()
        ]
        return problem_response(422, "validation_failed", "the request is not valid", errors=errors)

    async def on_http(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        code = _STATUS_CODES.get(exc.status_code, "error")
        detail = str(exc.detail) if exc.detail else HTTPStatus(exc.status_code).phrase
        headers = dict(exc.headers) if exc.headers else None
        return problem_response(exc.status_code, code, detail, headers=headers)

    async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "api.unhandled",
            path=request.url.path,
            error_type=type(exc).__name__,
            exc_info=exc,
        )
        return problem_response(500, "internal", "something went wrong on our side")

    app.add_exception_handler(ProblemError, on_problem)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(Exception, on_unexpected)
