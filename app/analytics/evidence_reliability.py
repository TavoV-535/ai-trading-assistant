"""
The Evidence Reliability Engine.

Per PROJECT.md's Milestone 12 spec: "Track long-term performance for
every evidence source... The system should learn historical reliability
statistics without modifying the original evidence." Not a plugin — a
core service, the same tier as the Decision Timeline. Never touches an
``Evidence`` object or a ``DecisionRecorded`` event — only reads
``DecisionRecorded`` and derives a running, per-source agreement count.

A source's stated direction (parsed from its evidence line — see
``app/evidence/formatting.py``) is "reliable" for one resolved decision
when it matches the *true* market direction that decision's outcome
reveals — not merely whether the decision's own ``simulated_action`` was
correct. This is derived, never asserted: if a decision's implied
direction turned out ``"correct"``, the true direction is that implied
direction; if ``"incorrect"``, the true direction is the opposite. A
``"neutral"`` outcome (or a still-pending decision) carries no reliability
signal for either direction and is skipped.

Future weighting systems (a future revision of the Confidence Weighting
Framework, ``app/aggregation/weighting.py``) can consume :meth:`ranked`/
:meth:`for_source` as a new factor — this engine only ever produces
statistics, never adjusts a weight itself.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.event_bus.bus import EventBus
from app.event_bus.events import ACTION_DIRECTIONS, DecisionRecorded
from app.evidence.formatting import parse_evidence_line
from app.logging import get_logger

log = get_logger(__name__)

_OPPOSITE_DIRECTION: dict[str, str] = {"bullish": "bearish", "bearish": "bullish", "neutral": "neutral"}


class EvidenceStat(BaseModel):
    source: str
    correct: int
    total: int

    @property
    def reliability(self) -> float:
        return (self.correct / self.total) if self.total else 0.0


class EvidenceReliabilityEngine:
    """Maintains a running, per-evidence-source agreement count against
    each resolved decision's revealed true market direction. Attach once
    at bootstrap (or once per Simulation Engine run); every consumer (the
    Learning Engine, the Knowledge Graph Query Layer, ``/coach``, and a
    future weighting system) reads it via :meth:`ranked`/:meth:`for_source`,
    the same read-only-query pattern every other core engine here
    exposes."""

    def __init__(self, settings: Any) -> None:
        self._correct: dict[str, int] = defaultdict(int)
        self._total: dict[str, int] = defaultdict(int)
        self._total_observed = 0
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="evidence_reliability_decisions")
        log.info("evidence_reliability_engine_attached")

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        if event.outcome_pending or event.outcome not in ("correct", "incorrect"):
            return
        implied = ACTION_DIRECTIONS.get(event.simulated_action)
        if implied is None:
            return
        true_direction = implied if event.outcome == "correct" else _OPPOSITE_DIRECTION.get(implied, implied)

        for line in list(event.technical_evidence) + list(event.fundamental_evidence):
            parsed = parse_evidence_line(line)
            if parsed is None:
                continue
            self._total[parsed.source] += 1
            if parsed.direction == true_direction:
                self._correct[parsed.source] += 1
        self._total_observed += 1

    # ---------------------------------------------------------------- queries

    def for_source(self, source: str) -> EvidenceStat | None:
        if source not in self._total:
            return None
        return EvidenceStat(source=source, correct=self._correct[source], total=self._total[source])

    def all(self) -> list[EvidenceStat]:
        return [EvidenceStat(source=s, correct=self._correct[s], total=self._total[s]) for s in sorted(self._total)]

    def ranked(self, *, top_n: int | None = None, min_sample: int = 1) -> list[EvidenceStat]:
        """Every tracked source, most reliable first. ``min_sample``
        excludes sources with too little history to be meaningful yet."""
        stats = [s for s in self.all() if s.total >= min_sample]
        stats.sort(key=lambda s: s.reliability, reverse=True)
        return stats[:top_n] if top_n is not None else stats

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "sources_tracked": len(self._total)}

    def diagnostics(self) -> dict[str, Any]:
        return {"total_observed": self._total_observed, "sources_tracked": sorted(self._total.keys())}

    def statistics(self) -> dict[str, Any]:
        return {
            "total_observed": self._total_observed,
            "sources_tracked": len(self._total),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
