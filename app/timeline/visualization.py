"""
Timeline visualization data.

Per PROJECT.md's Milestone 12 spec: "Extend the Decision Timeline to
expose structured timeline data suitable for future web dashboards...
without introducing dashboard dependencies." This module adds no new
subscriptions and no new dependency on any rendering library — it's a
pure, deterministic function that unifies already-built read shapes
(:class:`~app.timeline.models.DecisionRecord`,
:class:`~app.reflection.models.ReflectionRecord`,
:class:`~app.journal.models.JournalNote`,
:class:`~app.event_bus.events.RiskEvent`,
:class:`~app.event_bus.events.CoachingEvent`) into one ordered,
JSON-serializable list a future HTTP endpoint or dashboard can render
directly, without this codebase taking on any UI/dashboard dependency
itself.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.event_bus.events import CoachingEvent, RiskEvent
from app.timeline.models import DecisionRecord

if TYPE_CHECKING:
    # Type-only, to break a circular import: app.journal.models imports
    # app.timeline.models, which (via app.timeline/__init__.py importing
    # app.timeline.engine, which imports this module) would otherwise
    # re-enter app.journal.models while it's still mid-initialization.
    # `from __future__ import annotations` makes every annotation below a
    # lazily-evaluated string, so this costs nothing at runtime -- this
    # function only ever accesses attributes on the objects it's given, it
    # never constructs or isinstance-checks these types.
    from app.journal.models import JournalNote
    from app.reflection.models import ReflectionRecord

TimelineEntryType = Literal[
    "decision", "evidence", "strategy_match", "risk_event", "reflection", "journal", "outcome", "coaching_event"
]


class TimelineEntry(BaseModel):
    """One point on a symbol's unified timeline — deliberately flat and
    plain (only JSON-safe types) so it needs no adaptation before a
    future dashboard or HTTP API serializes it directly."""

    entry_type: TimelineEntryType
    timestamp: datetime
    symbol: str
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)


def build_symbol_timeline(
    symbol: str,
    *,
    decisions: list[DecisionRecord],
    reflections: list[ReflectionRecord] = (),
    journal_notes: list[JournalNote] = (),
    risk_events: list[RiskEvent] = (),
    coaching_events: list[CoachingEvent] = (),
) -> list[TimelineEntry]:
    """Composes already-fetched data (from ``DecisionTimeline``,
    ``ReflectionEngine``, ``TradingJournal``, the Capital Protection
    Engine, and the Learning Engine — or their durable
    ``EventLogRepository`` equivalents) into one chronologically ordered
    timeline for ``symbol``. A pure function of its inputs — never reaches
    into any engine itself — so it works identically whether the caller
    sourced its arguments from live in-memory engines or a durable
    database query."""
    entries: list[TimelineEntry] = []

    for decision in decisions:
        entries.append(
            TimelineEntry(
                entry_type="decision",
                timestamp=decision.timestamp,
                symbol=symbol,
                summary=f"{decision.simulated_action} (confidence {decision.confidence:.0f}/100)",
                detail={"event_id": str(decision.event_id), "reasoning_summary": decision.reasoning_summary},
            )
        )
        for line in decision.technical_evidence + decision.fundamental_evidence:
            entries.append(
                TimelineEntry(entry_type="evidence", timestamp=decision.timestamp, symbol=symbol, summary=line, detail={"decision_event_id": str(decision.event_id)})
            )
        for strategy in decision.strategy_matches:
            entries.append(
                TimelineEntry(
                    entry_type="strategy_match", timestamp=decision.timestamp, symbol=symbol, summary=strategy,
                    detail={"decision_event_id": str(decision.event_id)},
                )
            )
        if not decision.outcome_pending and decision.outcome is not None:
            entries.append(
                TimelineEntry(
                    entry_type="outcome", timestamp=decision.timestamp, symbol=symbol, summary=decision.outcome,
                    detail={"decision_event_id": str(decision.event_id), "outcome_price_change_pct": decision.outcome_price_change_pct},
                )
            )

    for reflection in reflections:
        entries.append(
            TimelineEntry(
                entry_type="reflection", timestamp=reflection.timestamp, symbol=symbol,
                summary=reflection.lessons_learned or reflection.reasoning,
                detail={"decision_event_id": str(reflection.decision_event_id), "potential_improvements": reflection.potential_improvements},
            )
        )

    for note in journal_notes:
        entries.append(TimelineEntry(entry_type="journal", timestamp=note.added_at, symbol=symbol, summary=note.text, detail={"author": note.author}))

    for risk_event in risk_events:
        entries.append(
            TimelineEntry(
                entry_type="risk_event", timestamp=risk_event.timestamp, symbol=symbol, summary=risk_event.message,
                detail={"risk_type": risk_event.risk_type, "severity": risk_event.severity, "value": risk_event.value},
            )
        )

    for coaching_event in coaching_events:
        entries.append(
            TimelineEntry(
                entry_type="coaching_event", timestamp=coaching_event.timestamp, symbol=symbol, summary=coaching_event.title,
                detail={"pattern_type": coaching_event.pattern_type, "priority": coaching_event.priority},
            )
        )

    entries.sort(key=lambda e: e.timestamp)
    return entries
