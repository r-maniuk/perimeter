"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from perimeter.api.errors import unauthorized
from perimeter.api.security import AuthError, Principal, request_token
from perimeter.api.state import AppState, state_of


def app_state(request: Request) -> AppState:
    return state_of(request)


State = Annotated[AppState, Depends(app_state)]


# Declares the scheme in the OpenAPI document (the reference's "Authorize"); the token itself is
# read below, from the bearer header or from the dashboard's session cookie.
SESSION_TOKEN = HTTPBearer(
    auto_error=False,
    scheme_name="SessionToken",
    description="A token from `POST /v1/token`; browsers send the session cookie instead.",
)


def current_principal(
    request: Request,
    state: State,
    _scheme: Annotated[HTTPAuthorizationCredentials | None, Security(SESSION_TOKEN)],
) -> Principal:
    token = request_token(request)
    if token is None:
        raise unauthorized()
    try:
        principal = state.tokens.verify(token)
    except AuthError as exc:
        raise unauthorized(str(exc)) from exc
    if state.revoked.is_revoked(principal.token_id):
        raise unauthorized("this session was signed out")
    return principal


CurrentUser = Annotated[Principal, Depends(current_principal)]
