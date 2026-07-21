"""
Database engine + session management.

PostgreSQL, SQLAlchemy 2.0 (async), Alembic for migrations, Repository
Pattern for access — no raw SQL anywhere in the codebase.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.logging import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base every ORM model inherits from."""


class Database:
    """Owns the engine + session factory for the process lifetime."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        engine_kwargs: dict[str, Any] = {"echo": settings.database.echo}
        # SQLite (used in tests / lightweight dev) uses StaticPool and doesn't
        # accept pool_size/max_overflow — only pass them for real server DBs.
        if not settings.database_url.startswith("sqlite"):
            engine_kwargs["pool_size"] = settings.database.pool_size
            engine_kwargs["max_overflow"] = settings.database.max_overflow
        self.engine: AsyncEngine = create_async_engine(settings.database_url, **engine_kwargs)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session; commits on success, rolls back on any exception."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        """Dev/test convenience — Alembic migrations are the source of truth in real deployments."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def health(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.run_sync(lambda sync_conn: None)
            return True
        except Exception:
            log.exception("database_health_check_failed")
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
        log.info("database_disposed")
