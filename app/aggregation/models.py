"""
Data shapes produced by the Evidence Aggregator. See
``app/aggregation/aggregator.py`` for the logic that builds these.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.event_bus.events import WeightedEvidenceEvent
from app.evidence.schema import Evidence


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EnrichmentInfo(BaseModel):
    """Aggregation metadata about one specific piece of evidence — attached
    to the ``EvidenceAggregated`` event alongside the original, unmodified
    ``Evidence`` object. Nothing here changes the evidence itself; it's
    context about how the aggregator is currently treating it."""

    model_config = ConfigDict(frozen=True)

    #: ``f"{source}:{title}"`` — how repeated confirmations of the exact
    #: same finding are grouped and deduplicated.
    group_key: str
    #: How many times this exact group has fired within the current
    #: freshness window, including this occurrence.
    occurrence_count: int
    #: True when a fresher instance of this same group already existed —
    #: i.e. this is a repeated confirmation, not the first sighting.
    is_duplicate: bool
    #: 1.0 when just published, decaying linearly to 0.0 at the freshness
    #: window boundary. Never negative.
    freshness: float
    #: Convenience — ``freshness > 0``.
    is_fresh: bool
    first_seen_at: datetime
    age_seconds: float


class AggregateSnapshot(BaseModel):
    """The Evidence Aggregator's current, deduped view of one symbol —
    exactly what gets attached to ``EvidenceAggregated.active_evidence``,
    plus a few summary counts for convenience. Fetchable directly via
    ``EvidenceAggregator.snapshot(symbol)`` without needing to listen to
    the event stream."""

    symbol: str
    active_evidence: list[Evidence] = Field(default_factory=list)
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    has_conflict: bool = False
    #: Confidence Weighting Framework output for each item in
    #: ``active_evidence``, same order — see ``app/aggregation/weighting.py``.
    #: The raw ``active_evidence`` list above is untouched; this is a
    #: parallel, explainable annotation, not a replacement.
    weighted_evidence: list[WeightedEvidenceEvent] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utcnow)
