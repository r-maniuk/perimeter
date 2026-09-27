"""``/v1/sessions``: the caller's live sessions on every replica, and remote sign-out.

Signing a session out revokes the token it was opened with (every replica then rejects that token,
on REST and on sockets) and tells the replica holding the socket to close it with 4001.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from nats.errors import Error as NatsError
from pydantic import BaseModel, Field

from perimeter.api.deps import CurrentUser, State
from perimeter.api.errors import ProblemError, not_found

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionOut(BaseModel):
    sid: str
    label: str = Field(description="Browser and operating system, for example 'Chrome · macOS'")
    agent: str | None = Field(description="User-Agent the socket was opened with")
    ip: str | None
    replica: str = Field(description="API replica holding the socket")
    connected_at: datetime
    current: bool = Field(description="Opened with the same sign-in as this request")


class SessionList(BaseModel):
    sessions: list[SessionOut]


def _unavailable(exc: Exception) -> ProblemError:
    return ProblemError(
        503,
        "unavailable",
        "the session registry is unavailable, retry shortly",
        headers={"Retry-After": "2"},
        extra={"error": type(exc).__name__},
    )


@router.get("", summary="Live sessions of the signed-in user, on every replica")
async def list_sessions(state: State, principal: CurrentUser) -> SessionList:
    records = state.registry.sessions_of(principal.user_id)
    return SessionList(
        sessions=[
            SessionOut(
                sid=record.sid,
                label=record.label,
                agent=record.agent,
                ip=record.ip,
                replica=record.replica,
                connected_at=record.connected_at,
                current=record.jti == principal.token_id,
            )
            for record in records
        ]
    )


@router.delete(
    "/{sid}",
    status_code=204,
    summary="Sign out one of the user's sessions (remote sign-out)",
    responses={404: {"description": "No such session of the signed-in user"}},
)
async def sign_out_session(sid: uuid.UUID, state: State, principal: CurrentUser) -> None:
    try:
        record = await state.registry.lookup(principal.user_id, str(sid))
        if record is None:
            raise not_found("session")
        await state.revoked.revoke(record.jti)
        await state.registry.terminate(record)
    except NatsError as exc:
        raise _unavailable(exc) from exc
