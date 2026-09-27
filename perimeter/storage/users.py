"""User accounts. The mocked sign-in only needs get-or-create by name and lookup by id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: uuid.UUID
    username: str
    created_at: datetime


_INSERT = text(
    """
    INSERT INTO users (username) VALUES (:username)
    ON CONFLICT (username) DO NOTHING
    RETURNING id, username, created_at
    """
)

_BY_NAME = text("SELECT id, username, created_at FROM users WHERE username = :username")

_BY_ID = text("SELECT id, username, created_at FROM users WHERE id = :id")


async def get_or_create(conn: AsyncConnection, username: str) -> tuple[UserRecord, bool]:
    """The user called ``username``, created on first use; the flag says whether it was created.

    Race-free without retries: ``ON CONFLICT DO NOTHING`` waits for a concurrent insert of the
    same name to commit and then inserts nothing, and the follow-up ``SELECT`` is a new statement,
    so under READ COMMITTED it runs on a fresh snapshot that already contains the winner's row.
    """
    row = (await conn.execute(_INSERT, {"username": username})).one_or_none()
    if row is not None:
        return UserRecord(*row), True
    row = (await conn.execute(_BY_NAME, {"username": username})).one()
    return UserRecord(*row), False


async def get(conn: AsyncConnection, user_id: uuid.UUID) -> UserRecord | None:
    row = (await conn.execute(_BY_ID, {"id": user_id})).one_or_none()
    return UserRecord(*row) if row is not None else None
