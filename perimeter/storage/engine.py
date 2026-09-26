"""Database engine factory.

Pools are deliberately small, bounded and fail fast: ``max_overflow=0`` means the pool never grows
past ``pool_size``, and a short checkout timeout turns exhaustion into an immediate, visible error
(HTTP 503 / a retried batch) instead of a silent pile-up of waiting coroutines. Server-side
timeouts back that up, so one runaway statement cannot hold a connection hostage.
"""

from __future__ import annotations

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from perimeter.config import DatabaseSettings


def database_url(settings: DatabaseSettings) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=settings.user,
        password=settings.password.get_secret_value() or None,
        host=settings.host,
        port=settings.port,
        database=settings.name,
    )


def create_engine(
    settings: DatabaseSettings,
    *,
    application_name: str,
    pool_size: int | None = None,
) -> AsyncEngine:
    size = pool_size or settings.pool_size
    return create_async_engine(
        database_url(settings),
        pool_size=size,
        max_overflow=0,
        pool_timeout=settings.pool_timeout_s,
        pool_pre_ping=True,
        pool_recycle=1_800,
        pool_use_lifo=True,
        connect_args={
            "timeout": 10,
            "command_timeout": settings.statement_timeout_ms / 1000 + 5,
            "ssl": settings.ssl,
            "server_settings": {
                "application_name": application_name,
                "statement_timeout": str(settings.statement_timeout_ms),
                "lock_timeout": "5000",
                "idle_in_transaction_session_timeout": "30000",
                "timezone": "UTC",
            },
        },
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
