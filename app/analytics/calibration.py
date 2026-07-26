"""
The Confidence Calibration system.

Per PROJECT.md's Milestone 12 spec: "The assistant should know whether its
own confidence estimates are optimistic or conservative." Not a plugin —
a core service, the same tier as the Decision Timeline. Subscribes to
``DecisionRecorded`` and buckets every *resolved*, directional decision
(``outcome`` is ``"correct"`` or ``"incorrect"`` — a ``"neutral"`` outcome
or a still-``outcome_pending`` decision carries no calibration signal and
is skipped) by its confidence into ten-point buckets, comparing each
bucket's actual win rate against the confidence it implied.

    90% confidence predictions -> historical outcomes -> actual win rate -> calibration report

Deterministic arithmetic only — counts and rates, never a model fit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded
from app.logging import get_logger

log = get_logger(__name__)

_BUCKET_WIDTH = 10
_DEFAULT_MIN_SAMPLE = 3
_DEFAULT_TOLERANCE = 0.10


class CalibrationBucket(BaseModel):
    label: str
    lower: float
    upper: float
    expected_rate: float
    actual_win_rate: float
    sample_size: int
    #: "overconfident" | "underconfident" | "well_calibrated" | "insufficient_data"
    verdict: str


class CalibrationReport(BaseModel):
    buckets: list[CalibrationBucket] = Field(default_factory=list)
    overall_verdict: str = "Not enough resolved decisions yet to assess calibration."
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConfidenceCalibrationService:
    """Maintains bucketed confidence-vs-outcome history and produces a
    :class:`CalibrationReport` on demand. Attach once at bootstrap (or once
    per Simulation Engine run); every consumer (the Learning Engine,
    ``/coach``, the Knowledge Graph Query Layer) reads it via
    :meth:`report`, the same read-only-query pattern every other core
    engine in this codebase exposes."""

    def __init__(self, settings: Any, *, min_sample: int | None = None, tolerance: float | None = None) -> None:
        section = getattr(settings, "learning", None)
        self._min_sample = min_sample if min_sample is not None else int(getattr(section, "calibration_min_sample", _DEFAULT_MIN_SAMPLE))
        self._tolerance = tolerance if tolerance is not None else float(getattr(section, "calibration_tolerance", _DEFAULT_TOLERANCE))
        self._bucket_outcomes: dict[int, list[bool]] = defaultdict(list)
        self._total_observed = 0
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="confidence_calibration_decisions")
        log.info("confidence_calibration_attached", min_sample=self._min_sample, tolerance=self._tolerance)

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        if event.outcome_pending or event.outcome not in ("correct", "incorrect"):
            return
        bucket = min(90, max(0, int(event.confidence // _BUCKET_WIDTH) * _BUCKET_WIDTH))
        self._bucket_outcomes[bucket].append(event.outcome == "correct")
        self._total_observed += 1

    # ---------------------------------------------------------------- queries

    def report(self) -> CalibrationReport:
        buckets: list[CalibrationBucket] = []
        weighted_gaps: list[tuple[int, float]] = []
        for lo in range(0, 100, _BUCKET_WIDTH):
            samples = self._bucket_outcomes.get(lo, [])
            total = len(samples)
            expected = (lo + _BUCKET_WIDTH / 2) / 100.0
            if total < self._min_sample:
                buckets.append(
                    CalibrationBucket(
                        label=f"{lo}-{lo + _BUCKET_WIDTH}%", lower=lo, upper=lo + _BUCKET_WIDTH,
                        expected_rate=expected, actual_win_rate=0.0, sample_size=total, verdict="insufficient_data",
                    )
                )
                continue
            actual = sum(samples) / total
            gap = actual - expected
            if gap < -self._tolerance:
                verdict = "overconfident"
            elif gap > self._tolerance:
                verdict = "underconfident"
            else:
                verdict = "well_calibrated"
            buckets.append(
                CalibrationBucket(
                    label=f"{lo}-{lo + _BUCKET_WIDTH}%", lower=lo, upper=lo + _BUCKET_WIDTH,
                    expected_rate=expected, actual_win_rate=actual, sample_size=total, verdict=verdict,
                )
            )
            weighted_gaps.append((total, gap))

        if not weighted_gaps:
            overall = "Not enough resolved decisions yet to assess calibration."
        else:
            weighted_gap = sum(t * g for t, g in weighted_gaps) / sum(t for t, _ in weighted_gaps)
            if weighted_gap < -self._tolerance:
                overall = f"Overall, confidence estimates run optimistic (overconfident) by about {abs(weighted_gap):.0%}."
            elif weighted_gap > self._tolerance:
                overall = f"Overall, confidence estimates run conservative (underconfident) by about {weighted_gap:.0%}."
            else:
                overall = "Overall, confidence estimates are reasonably well-calibrated."

        return CalibrationReport(buckets=buckets, overall_verdict=overall)

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "total_observed": self._total_observed}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "total_observed": self._total_observed,
            "buckets_with_data": sorted(self._bucket_outcomes.keys()),
            "min_sample": self._min_sample,
            "tolerance": self._tolerance,
        }

    def statistics(self) -> dict[str, Any]:
        return {"total_observed": self._total_observed, "generated_at": datetime.now(timezone.utc).isoformat()}
