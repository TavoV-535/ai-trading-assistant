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
from app.event_bus.events import DecisionRecorded, Event, ReflectionGenerated, RiskEvent
from app.journal.models import JournalNote
from app.logging import get_logger
from app.reflection.models import ReflectionRecord
from app.timeline.models import DecisionRecord

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

    async def decision_records(self, *, symbol: str | None = None, limit: int = 100) -> list[DecisionRecord]:
        """The Decision Timeline's durable, unbounded history — every
        ``DecisionRecorded`` event ever published, reconstructed straight
        from its already-persisted ``event_log`` row (no separate table;
        see ``app/timeline/engine.py``'s module docstring for why).

        Filters by ``symbol`` in Python after a bounded DB fetch rather
        than a JSON-containment SQL clause — this codebase's generic
        ``Repository`` deliberately never hand-builds dialect-specific SQL
        (no raw SQL anywhere), and the volumes this method serves today
        (an in-process query, not a public API under load) don't yet
        justify a dedicated indexed column. A future milestone can add one
        if querying performance at scale ever requires it -- a documented,
        deferred scope boundary, the same pattern this codebase uses
        throughout (see e.g. the Confidence Weighting Framework's
        correlation-dampening proxy)."""
        # Fetch a generous page so Python-side symbol filtering still
        # yields up to `limit` matches in the common case without needing
        # to page repeatedly; recent() already bounds this query's cost.
        rows = await self.recent(event_type="DecisionRecorded", limit=max(limit * 5, limit))
        records: list[DecisionRecord] = []
        for row in rows:
            if symbol is not None and row.payload.get("symbol") != symbol:
                continue
            event = DecisionRecorded.model_validate({**row.payload, "event_id": row.event_id, "timestamp": row.created_at, "source": row.source, "correlation_id": row.correlation_id})
            records.append(DecisionRecord.from_event(event))
            if len(records) >= limit:
                break
        return records

    async def reflections(self, *, symbol: str | None = None, limit: int = 100) -> list[ReflectionRecord]:
        """The Reflection Engine's durable, unbounded history — every
        ``ReflectionGenerated`` event ever published, reconstructed
        straight from its already-persisted ``event_log`` row. Mirrors
        :meth:`decision_records` exactly — same Python-side symbol
        filtering, same documented deferred-scope reasoning (see that
        method's docstring)."""
        rows = await self.recent(event_type="ReflectionGenerated", limit=max(limit * 5, limit))
        records: list[ReflectionRecord] = []
        for row in rows:
            if symbol is not None and row.payload.get("symbol") != symbol:
                continue
            event = ReflectionGenerated.model_validate({**row.payload, "event_id": row.event_id, "timestamp": row.created_at, "source": row.source, "correlation_id": row.correlation_id})
            records.append(ReflectionRecord.from_event(event))
            if len(records) >= limit:
                break
        return records

    async def journal_notes(
        self, *, symbol: str | None = None, decision_event_id: Any | None = None, limit: int = 100
    ) -> list[JournalNote]:
        """The Trading Journal's durable, unbounded note/screenshot
        history — every ``JournalCreated`` event ever published,
        reconstructed straight from its already-persisted ``event_log``
        row. ``decision_event_id`` (if given) further narrows to notes
        attached to one specific decision; omitted, it returns every note
        for ``symbol`` regardless of which decision (or none) it's
        attached to. Mirrors :meth:`decision_records` exactly."""
        rows = await self.recent(event_type="JournalCreated", limit=max(limit * 5, limit))
        notes: list[JournalNote] = []
        for row in rows:
            if symbol is not None and row.payload.get("symbol") != symbol:
                continue
            if decision_event_id is not None and row.payload.get("decision_event_id") != str(decision_event_id):
                continue
            if not row.payload.get("note"):
                continue
            notes.append(
                JournalNote(text=row.payload["note"], author=row.payload.get("author"), added_at=row.created_at)
            )
            if len(notes) >= limit:
                break
        return notes

    async def risk_events(
        self, *, symbol: str | None = None, risk_type: str | None = None, limit: int = 100
    ) -> list[RiskEvent]:
        """The Capital Protection Engine's durable, unbounded history —
        every ``RiskEvent`` ever published, reconstructed straight from
        its already-persisted ``event_log`` row. Mirrors
        :meth:`decision_records` exactly — same Python-side symbol
        filtering, plus an optional further narrowing by ``risk_type``
        (one of ``app.event_bus.events.RISK_TYPES``). Unlike
        ``DecisionRecord``/``ReflectionRecord``, no separate query-facing
        wrapper class exists — ``RiskEvent``'s own fields are already a
        stable, self-contained read shape, so this reconstructs the event
        itself rather than a parallel model."""
        rows = await self.recent(event_type="RiskEvent", limit=max(limit * 5, limit))
        events: list[RiskEvent] = []
        for row in rows:
            if symbol is not None and row.payload.get("symbol") != symbol:
                continue
            if risk_type is not None and row.payload.get("risk_type") != risk_type:
                continue
            event = RiskEvent.model_validate(
                {**row.payload, "event_id": row.event_id, "timestamp": row.created_at, "source": row.source, "correlation_id": row.correlation_id}
            )
            events.append(event)
            if len(events) >= limit:
                break
        return events

    async def by_event_id(self, event_id: Any) -> EventLog | None:
        """Fetches the one durable row whose own ``event_id`` (the UUID
        every event carries, unique+indexed on this table) matches --
        distinct from :meth:`get`, which looks up the internal
        auto-increment primary key. This is what the Event Replay API
        (``app/replay/``, Milestone 12) uses to fetch the ``DecisionRecorded``
        row a ``decision_event_id`` on another event points back to."""
        stmt = select(EventLog).where(EventLog.event_id == event_id).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def related_to_decision(self, decision_event_id: Any, *, event_types: tuple[str, ...] | None = None, limit: int = 200) -> list[EventLog]:
        """Every durable row whose payload carries this ``decision_event_id``
        -- ``TradeOpened``/``TradeClosed``/``ReflectionGenerated``/
        ``JournalCreated`` today, the only event types that reference one.
        Python-side filtering, same documented deferred-scope reasoning as
        :meth:`decision_records` (no dedicated indexed column for a JSON
        field yet). Used by the Event Replay API to reconstruct a
        complete historical decision."""
        types = event_types or ("TradeOpened", "TradeClosed", "ReflectionGenerated", "JournalCreated")
        target = str(decision_event_id)
        matched: list[EventLog] = []
        for event_type in types:
            rows = await self.recent(event_type=event_type, limit=limit)
            matched.extend(row for row in rows if row.payload.get("decision_event_id") == target)
        matched.sort(key=lambda r: r.created_at)
        return matched
