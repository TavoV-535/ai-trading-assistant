"""
The Event Replay API.

One of the Milestone 12 architectural recommendations: "Build an Event
Replay API capable of reconstructing any decision from historical events
for debugging, coaching, and future visualizations." Not a plugin — a
core service, the same tier as the Decision Timeline. Purely a read
surface over the durable event log (``EventLogRepository``, via the
Repository pattern — this module never imports SQLAlchemy or builds a
query itself) — it can never alter history, only reconstruct a view of
it, exactly like ``EventLogRepository.decision_records()`` and friends
already do for their respective engines.

A "decision" is reconstructed by ``decision_event_id`` (the UUID other
events reference back to the triggering ``DecisionRecorded`` — see that
event's docstring) rather than ``correlation_id``, which in this codebase
identifies an entire *simulation run*, not one decision (every
``DecisionRecorded`` in one ``SimulationEngine.run()`` shares the same
``correlation_id`` — see ``app/simulation/engine.py``). ``RiskEvent`` is
deliberately not part of a single decision's replay: it is a portfolio/
symbol-level evaluation, not decision-scoped (see ``RiskEvent``'s
docstring) — a future enhancement could nearest-match one by timestamp,
but that would be an approximation this module doesn't want to silently
imply is exact.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.repository import EventLogRepository
from app.event_bus.events import EVENT_TYPES, JournalCreated, ReflectionGenerated, TradeClosed, TradeOpened
from app.journal.models import JournalNote
from app.logging import get_logger
from app.reflection.models import ReflectionRecord
from app.timeline.models import DecisionRecord

if TYPE_CHECKING:
    from app.db.base import Database

log = get_logger(__name__)


class ReplayedEvent(BaseModel):
    """One raw event in a decision's causal chain — kept generic (event
    type + timestamp + source + a short summary) rather than typed per
    event, since the whole point of ``timeline`` is a uniform, orderable
    trace regardless of which event type produced each entry."""

    event_type: str
    timestamp: Any
    source: str | None = None
    summary: str = ""


class DecisionReplay(BaseModel):
    """A complete historical decision, reconstructed from the durable
    event log. ``decision`` is ``None`` if the ``decision_event_id`` given
    doesn't match any durable ``DecisionRecorded`` row — an honest "not
    found," never a fabricated placeholder."""

    decision_event_id: UUID
    decision: DecisionRecord | None = None
    reflection: ReflectionRecord | None = None
    journal_notes: list[JournalNote] = Field(default_factory=list)
    trade_opened: TradeOpened | None = None
    trade_closed: TradeClosed | None = None
    timeline: list[ReplayedEvent] = Field(default_factory=list)


class EventReplayService:
    """Reconstructs a complete historical decision from the durable event
    log on demand. Attach isn't needed — this service never subscribes to
    the bus itself, it only reads ``EventLogRepository`` (via the
    ``Database`` it's constructed with) when :meth:`replay_decision` is
    called."""

    def __init__(self, database: "Database") -> None:
        self._database = database
        self._total_replays = 0

    async def replay_decision(self, decision_event_id: UUID) -> DecisionReplay:
        self._total_replays += 1
        async with self._database.session() as session:
            repo = EventLogRepository(session)
            decision_row = await repo.by_event_id(decision_event_id)
            decision: DecisionRecord | None = None
            timeline: list[ReplayedEvent] = []

            if decision_row is not None and decision_row.event_type == "DecisionRecorded":
                event_cls = EVENT_TYPES["DecisionRecorded"]
                event = event_cls.model_validate(
                    {**decision_row.payload, "event_id": decision_row.event_id, "timestamp": decision_row.created_at,
                     "source": decision_row.source, "correlation_id": decision_row.correlation_id}
                )
                decision = DecisionRecord.from_event(event)
                timeline.append(ReplayedEvent(event_type="DecisionRecorded", timestamp=decision_row.created_at, source=decision_row.source, summary=event.reasoning_summary))

            related_rows = await repo.related_to_decision(decision_event_id)
            reflection: ReflectionRecord | None = None
            journal_notes: list[JournalNote] = []
            trade_opened: TradeOpened | None = None
            trade_closed: TradeClosed | None = None

            for row in related_rows:
                event_cls = EVENT_TYPES.get(row.event_type)
                if event_cls is None:
                    continue
                event = event_cls.model_validate(
                    {**row.payload, "event_id": row.event_id, "timestamp": row.created_at, "source": row.source, "correlation_id": row.correlation_id}
                )
                summary = ""
                if isinstance(event, ReflectionGenerated):
                    reflection = ReflectionRecord.from_event(event)
                    summary = event.lessons_learned
                elif isinstance(event, JournalCreated):
                    if event.note:
                        journal_notes.append(JournalNote(text=event.note, author=event.author, added_at=row.created_at))
                        summary = event.note
                elif isinstance(event, TradeOpened):
                    trade_opened = event
                    summary = f"opened {event.side} {event.quantity:.4f} @ {event.entry_price:.2f}"
                elif isinstance(event, TradeClosed):
                    trade_closed = event
                    summary = f"closed @ {event.exit_price:.2f} pnl={event.pnl}"
                timeline.append(ReplayedEvent(event_type=row.event_type, timestamp=row.created_at, source=row.source, summary=summary))

            timeline.sort(key=lambda e: e.timestamp)
            return DecisionReplay(
                decision_event_id=decision_event_id,
                decision=decision,
                reflection=reflection,
                journal_notes=journal_notes,
                trade_opened=trade_opened,
                trade_closed=trade_closed,
                timeline=timeline,
            )

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        db_ok = await self._database.health()
        return {"status": "healthy" if db_ok else "degraded", "database_reachable": db_ok}

    def diagnostics(self) -> dict[str, Any]:
        return {"total_replays": self._total_replays}

    def statistics(self) -> dict[str, Any]:
        return {"total_replays": self._total_replays}
