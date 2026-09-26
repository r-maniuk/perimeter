"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from perimeter.api.errors import unauthorized
from perimeter.api.security import AuthError, Principal, request_token
from perimeter.api.state import AppState, state_of


def app_state(request: Request) -> AppState:
    return state_of(request)


State = Annotated[AppState, Depends(app_state)]


async def db_session(state: State) -> AsyncIterator[AsyncSession]:
    async with state.sessions() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(db_session)]


def current_principal(request: Request, state: State) -> Principal:
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
