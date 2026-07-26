"""
The Analytics Service.

One of the Milestone 12 architectural recommendations: "Create a
dedicated Analytics Service so statistics are calculated once and reused
by ``/analyze``, ``/coach``, ``/journal``, ``/watchlist``, and future
dashboards." Not a plugin — a core service, the same tier as the Decision
Timeline. Owns no state itself and performs no calculation of its own —
it is a thin, read-only facade over three independent services (Strategy
Analytics, Evidence Reliability, Confidence Calibration), each already
responsible for its own statistics. This is what "no duplicated
calculations" means structurally here: every command plugin reaches for
``AnalyticsService`` instead of each reimplementing its own win-rate/
reliability/calibration math.
"""
from __future__ import annotations

from typing import Any

from app.analytics.calibration import CalibrationReport, ConfidenceCalibrationService
from app.analytics.evidence_reliability import EvidenceReliabilityEngine, EvidenceStat
from app.analytics.strategy_analytics import StrategyAnalyticsService, StrategyStats


class AnalyticsService:
    """Constructed from already-built collaborators (never constructs its
    own copies) — the same "compose, don't duplicate" pattern
    ``KnowledgeGraphQueryEngine`` uses for its optional
    ``evidence_reliability``/``confidence_calibration`` collaborators."""

    def __init__(
        self,
        *,
        strategy_analytics: StrategyAnalyticsService,
        evidence_reliability: EvidenceReliabilityEngine,
        confidence_calibration: ConfidenceCalibrationService,
    ) -> None:
        self._strategy_analytics = strategy_analytics
        self._evidence_reliability = evidence_reliability
        self._confidence_calibration = confidence_calibration

    # ---------------------------------------------------------------- strategy analytics

    def strategy_stats(self, strategy: str) -> StrategyStats | None:
        return self._strategy_analytics.stats_for(strategy)

    def all_strategy_stats(self) -> list[StrategyStats]:
        return self._strategy_analytics.all()

    def strategies(self) -> list[str]:
        return self._strategy_analytics.strategies()

    # ---------------------------------------------------------------- evidence reliability

    def evidence_reliability_for(self, source: str) -> EvidenceStat | None:
        return self._evidence_reliability.for_source(source)

    def ranked_evidence_reliability(self, *, top_n: int | None = None, min_sample: int = 1) -> list[EvidenceStat]:
        return self._evidence_reliability.ranked(top_n=top_n, min_sample=min_sample)

    # ---------------------------------------------------------------- confidence calibration

    def calibration_report(self) -> CalibrationReport:
        return self._confidence_calibration.report()

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "strategy_analytics": await self._strategy_analytics.health(),
            "evidence_reliability": await self._evidence_reliability.health(),
            "confidence_calibration": await self._confidence_calibration.health(),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "strategy_analytics": self._strategy_analytics.diagnostics(),
            "evidence_reliability": self._evidence_reliability.diagnostics(),
            "confidence_calibration": self._confidence_calibration.diagnostics(),
        }

    def statistics(self) -> dict[str, Any]:
        return {
            "strategy_analytics": self._strategy_analytics.statistics(),
            "evidence_reliability": self._evidence_reliability.statistics(),
            "confidence_calibration": self._confidence_calibration.statistics(),
        }
