"""
Milestone 1 ORM models.

Only the durable audit trail lives here for now — ``EventLog`` persists
every event that crosses the bus, which is what "everything logged" means
at the database layer. Domain tables (trades, journals, watchlists,
strategies, ...) arrive in their own milestones, each adding models here
without touching anything above.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.db.base import Base


class EventLog(Base):
    """One row per event published on the Event Bus."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<EventLog id={self.id} type={self.event_type!r} source={self.source!r}>"
