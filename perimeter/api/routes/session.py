"""Sign-in, sign-out and the current user.

The brief allows mocked authentication: a username is exchanged for a signed session token, and
the account is created on first use. A browser signs in with ``POST /v1/session``: the token is
set as an ``HttpOnly`` cookie and appears in no response body, so no script on the page can read
it. A client without cookies asks ``POST /v1/token`` and sends the token it gets as a bearer
header; that sets no cookie. Either token signs out with ``DELETE /v1/session``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import nats.errors
from fastapi import APIRouter, Depends, Request, Response, status

from perimeter.api.deps import CurrentUser, State
from perimeter.api.errors import ProblemError, rate_limited, unauthorized
from perimeter.api.ratelimit import RateLimiter, client_key
from perimeter.api.schemas import Me, Session, SessionCreate, TokenGrant, User
from perimeter.api.security import COOKIE_NAME, AuthError, IssuedToken, request_token
from perimeter.api.state import AppState
from perimeter.storage import users

router = APIRouter(tags=["session"])

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "Not signed in, or the session was signed out"}
}


def login_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.login_limiter
    return limiter


def limit_logins(request: Request, limiter: Annotated[RateLimiter, Depends(login_limiter)]) -> None:
    wait_s = limiter.acquire(client_key(request.client.host if request.client else None))
    if wait_s > 0:
        raise rate_limited("too many sign-in attempts from this address; retry later", wait_s)


_SIGN_IN_LIMITED: dict[int | str, dict[str, Any]] = {
    429: {"description": "Too many sign-in attempts from this address"}
}


async def _issue(state: AppState, username: str) -> tuple[User, IssuedToken]:
    async with state.db.begin() as conn:
        user, _ = await users.get_or_create(conn, username)
    return User(id=user.id, username=user.username), state.tokens.issue(user.id, user.username)


@router.post(
    "/session",
    status_code=status.HTTP_201_CREATED,
    response_model=Session,
    dependencies=[Depends(limit_logins)],
    responses=_SIGN_IN_LIMITED,
    summary="Sign in from a browser: sets the session cookie (creates the user on first use)",
)
async def sign_in(body: SessionCreate, state: State, response: Response) -> Session:
    user, issued = await _issue(state, body.username)
    response.set_cookie(
        COOKIE_NAME,
        issued.token,
        max_age=state.tokens.ttl_s,
        path="/",
        httponly=True,
        samesite="lax",
        secure=state.settings.security.secure_cookies,
    )
    return Session(
        expires_at=datetime.fromtimestamp(issued.principal.expires_at, tz=UTC), user=user
    )


@router.post(
    "/token",
    status_code=status.HTTP_201_CREATED,
    response_model=TokenGrant,
    dependencies=[Depends(limit_logins)],
    responses=_SIGN_IN_LIMITED,
    summary="Sign in without cookies: a bearer token (creates the user on first use)",
)
async def issue_token(body: SessionCreate, state: State) -> TokenGrant:
    user, issued = await _issue(state, body.username)
    return TokenGrant(
        token=issued.token,
        expires_at=datetime.fromtimestamp(issued.principal.expires_at, tz=UTC),
        user=user,
    )


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out (revokes the token on every replica)",
)
async def sign_out(request: Request, state: State) -> Response:
    """Idempotent: a missing, expired or already revoked token still ends with a cleared cookie."""
    token = request_token(request)
    if token is not None:
        try:
            principal = state.tokens.verify(token)
        except AuthError:
            pass
        else:
            try:
                await state.revoked.revoke(principal.token_id)
            except nats.errors.Error as exc:
                # Other replicas would keep honouring the token: report failure, do not pretend.
                raise ProblemError(
                    503,
                    "revocation_unavailable",
                    "the sign-out could not be recorded; retry",
                    headers={"Retry-After": "1"},
                ) from exc
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=state.settings.security.secure_cookies,
    )
    return response


@router.get("/me", response_model=Me, responses=_UNAUTHORIZED, summary="The signed-in user")
async def me(principal: CurrentUser, state: State) -> Me:
    async with state.db.connect() as conn:
        user = await users.get(conn, principal.user_id)
    if user is None:
        raise unauthorized("this account no longer exists")
    return Me(
        id=user.id,
        username=user.username,
        created_at=user.created_at,
        session_expires_at=datetime.fromtimestamp(principal.expires_at, tz=UTC),
    )
