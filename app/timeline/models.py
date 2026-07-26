"""Data shapes produced by the Decision Timeline. See
``app/timeline/engine.py`` for the logic that builds these."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.event_bus.events import DecisionRecorded


class DecisionRecord(BaseModel):
    """One entry in the Decision Timeline — the query-facing shape,
    built from a ``DecisionRecorded`` event via :meth:`from_event`. Kept
    as its own model (rather than querying ``DecisionRecorded`` events
    directly) for the same reason ``SymbolProfile`` is its own model
    alongside ``SymbolProfileUpdated`` (Milestone 8): a stable read shape
    that doesn't change if the wire event's field set ever needs to grow
    for publishing reasons."""

    event_id: UUID
    timestamp: datetime
    correlation_id: UUID | None = None
    symbol: str
    market_context: dict[str, str] = Field(default_factory=dict)
    technical_evidence: list[str] = Field(default_factory=list)
    fundamental_evidence: list[str] = Field(default_factory=list)
    confidence_weights: dict[str, float] = Field(default_factory=dict)
    strategy_matches: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    reasoning_source: str = "insufficient_evidence"
    confidence: float = 0.0
    simulated_action: str = "no_action"
    price_at_decision: float | None = None
    bar_index: int = 0
    lookahead_bars: int = 0
    outcome: str | None = None
    outcome_price_change_pct: float | None = None
    outcome_pending: bool = True

    @classmethod
    def from_event(cls, event: DecisionRecorded) -> "DecisionRecord":
        return cls(
            event_id=event.event_id,
            timestamp=event.timestamp,
            correlation_id=event.correlation_id,
            symbol=event.symbol,
            market_context=dict(event.market_context),
            technical_evidence=list(event.technical_evidence),
            fundamental_evidence=list(event.fundamental_evidence),
            confidence_weights=dict(event.confidence_weights),
            strategy_matches=list(event.strategy_matches),
            reasoning_summary=event.reasoning_summary,
            reasoning_source=event.reasoning_source,
            confidence=event.confidence,
            simulated_action=event.simulated_action,
            price_at_decision=event.price_at_decision,
            bar_index=event.bar_index,
            lookahead_bars=event.lookahead_bars,
            outcome=event.outcome,
            outcome_price_change_pct=event.outcome_price_change_pct,
            outcome_pending=event.outcome_pending,
        )
