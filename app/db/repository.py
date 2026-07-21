"""
Generic Repository Pattern.

All database access goes through a ``Repository`` — no raw SQL, and no
plugin or engine ever imports SQLAlchemy directly. This is the only layer
allowed to build queries.
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.models import EventLog
from app.event_bus.events import Event
from app.logging import get_logger

ModelT = TypeVar("ModelT", bound=Base)

log = get_logger(__name__)


class Repository(Generic[ModelT]):
    """Generic CRUD repository for a single ORM model.

    Concrete repositories (e.g. :class:`EventLogRepository`) subclass this
    to add model-specific queries — the generic methods here cover the
    common case so most plugins never need to write their own.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession, model: type[ModelT] | None = None) -> None:
        self.session = session
        if model is not None:
            self.model = model

    async def add(self, obj: ModelT) -> ModelT:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def get(self, id_: Any) -> ModelT | None:
        return await self.session.get(self.model, id_)

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> list[ModelT]:
        stmt = select(self.model)
        for field, value in filters.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self.model)
        for field, value in filters.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def delete(self, obj: ModelT) -> None:
        await self.session.delete(obj)
        await self.session.flush()


class EventLogRepository(Repository[EventLog]):
    """Persists every event that crosses the Event Bus."""

    model = EventLog

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, EventLog)

    async def record_event(self, event: Event) -> EventLog:
        payload = event.model_dump(mode="json", exclude={"event_id", "timestamp", "source", "correlation_id"})
        row = EventLog(
            event_id=event.event_id,
            event_type=event.event_type,
            source=event.source,
            correlation_id=event.correlation_id,
            payload=payload,
            created_at=event.timestamp,
        )
        return await self.add(row)

    async def recent(self, *, event_type: str | None = None, limit: int = 50) -> list[EventLog]:
        stmt = select(EventLog).order_by(EventLog.created_at.desc()).limit(limit)
        if event_type:
            stmt = stmt.where(EventLog.event_type == event_type)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
