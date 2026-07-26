"""Data shapes produced by the Reflection Engine. See
``app/reflection/engine.py`` for the logic that builds these."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.event_bus.events import ReflectionGenerated


class ReflectionRecord(BaseModel):
    """One entry in a symbol's reflection history — the query-facing
    shape, built from a ``ReflectionGenerated`` event via
    :meth:`from_event`. Kept as its own model for the same reason
    ``DecisionRecord`` is its own model alongside ``DecisionRecorded``
    (Milestone 9): a stable read shape that doesn't change if the wire
    event's field set ever needs to grow for publishing reasons."""

    event_id: UUID
    timestamp: datetime
    correlation_id: UUID | None = None
    symbol: str
    decision_event_id: UUID
    reasoning: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)
    contradictory_evidence: list[str] = Field(default_factory=list)
    market_context: dict[str, str] = Field(default_factory=dict)
    confidence: float = 0.0
    confidence_evolution: str = "unknown"
    simulated_action: str = "no_action"
    outcome: str | None = None
    outcome_price_change_pct: float | None = None
    lessons_learned: str = ""
    potential_improvements: str = ""

    @classmethod
    def from_event(cls, event: ReflectionGenerated) -> "ReflectionRecord":
        return cls(
            event_id=event.event_id,
            timestamp=event.timestamp,
            correlation_id=event.correlation_id,
            symbol=event.symbol,
            decision_event_id=event.decision_event_id,
            reasoning=event.reasoning,
            supporting_evidence=list(event.supporting_evidence),
            contradictory_evidence=list(event.contradictory_evidence),
            market_context=dict(event.market_context),
            confidence=event.confidence,
            confidence_evolution=event.confidence_evolution,
            simulated_action=event.simulated_action,
            outcome=event.outcome,
            outcome_price_change_pct=event.outcome_price_change_pct,
            lessons_learned=event.lessons_learned,
            potential_improvements=event.potential_improvements,
        )
