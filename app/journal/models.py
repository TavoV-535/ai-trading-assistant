"""Data shapes produced by the Trading Journal. See
``app/journal/engine.py`` for the logic that builds these."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.timeline.models import DecisionRecord
from app.reflection.models import ReflectionRecord


class JournalNote(BaseModel):
    """One user-authored note, durable via ``JournalCreated`` (see
    ``app/event_bus/events.py``)."""

    text: str
    author: str | None = None
    added_at: datetime


class JournalEntry(BaseModel):
    """One enriched record in the Trading Journal.

    Deliberately **wraps** a :class:`~app.timeline.models.DecisionRecord`
    (and, once generated, its
    :class:`~app.reflection.models.ReflectionRecord`) rather than copying
    their fields onto a parallel schema — per the Milestone 10 spec, "the
    journal should enrich existing timeline records rather than
    duplicating them." Everything additive the Journal contributes lives
    alongside, not instead of, the Decision Timeline's own record:
    ``notes`` (user-authored), ``screenshots`` (placeholder URL/path
    strings — no image upload handling exists, and this doesn't pretend
    it does), and ``broker_execution`` (a deliberate placeholder for a
    future broker/paper-trading execution system, always ``None`` today,
    honestly)."""

    decision: DecisionRecord
    reflection: ReflectionRecord | None = None
    notes: list[JournalNote] = Field(default_factory=list)
    #: Placeholder support only, per the Milestone 10 spec — URL/path
    #: strings, never actual image bytes.
    screenshots: list[str] = Field(default_factory=list)
    #: Always None today — a documented, honest placeholder for a future
    #: broker/paper-trading execution system (Milestone 10 spec: "Future
    #: broker execution data"). Never fabricated.
    broker_execution: dict[str, Any] | None = None

    @property
    def symbol(self) -> str:
        return self.decision.symbol

    @property
    def decision_event_id(self) -> UUID:
        return self.decision.event_id
