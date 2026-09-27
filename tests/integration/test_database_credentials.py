"""PostgreSQL accepts the SCRAM verifiers written by the secrets job, for their password only."""

from __future__ import annotations

import secrets

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.config import DatabaseSettings
from perimeter.tools.secrets import scram_sha256


async def _login(settings: DatabaseSettings, user: str, password: str) -> str:
    conn = await asyncpg.connect(
        host=settings.host,
        port=settings.port,
        database=settings.name,
        user=user,
        password=password,
        timeout=10,
    )
    try:
        current: str = await conn.fetchval("SELECT current_user")
        return current
    finally:
        await conn.close()


async def test_a_role_created_from_a_verifier_logs_in_with_the_password(
    db: AsyncEngine, database_settings: DatabaseSettings
) -> None:
    role = f"verifier_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(32)
    async with db.begin() as conn:
        # A verifier is full of ':' that text() would read as bind parameters.
        await conn.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{scram_sha256(password)}'")
    try:
        assert await _login(database_settings, role, password) == role
        with pytest.raises(asyncpg.InvalidPasswordError):
            await _login(database_settings, role, secrets.token_urlsafe(32))
    finally:
        async with db.begin() as conn:
            await conn.exec_driver_sql(f"DROP ROLE {role}")
