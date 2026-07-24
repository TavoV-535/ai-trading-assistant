"""
The Confidence Weighting Framework.

Extends the Evidence Aggregator from "how many pieces of evidence exist"
to "how much should each piece of evidence actually count". This module
computes a single normalized ``weight`` in ``[0.0, 1.0]`` per piece of
evidence, built from several independently-documented, independently-
testable factors multiplied together. The **original Evidence object is
never modified, replaced, or discarded** — ``compute_weight`` only ever
produces an additional, parallel annotation (see
:class:`~app.event_bus.events.WeightedEvidenceEvent`) that enhances
reasoning without hiding the underlying evidence.

Every factor PROJECT.md's Milestone 7 spec asked for is represented below,
honestly labeled as either a real computed signal or a documented, tunable
proxy:

- **source reliability** — a per-source config multiplier
  (``settings.confidence_weighting.source_reliability``), defaulting to
  :data:`DEFAULT_SOURCE_RELIABILITY` for any source not explicitly tuned.
  This also stands in for **historical reliability** until a real trade
  journal / backtest outcome history exists (a later milestone) to derive
  it empirically instead of by config.
- **evidence freshness** — read straight from the aggregator's own
  ``EnrichmentInfo.freshness`` (see ``app/aggregation/aggregator.py``).
- **evidence persistence** — from ``EnrichmentInfo.occurrence_count``, with
  diminishing returns so a chronic false-positive source can't
  runaway-compound its own weight.
- **timeframe alignment** — how many other currently-active pieces of
  evidence for the same symbol share this one's timeframe.
- **market regime** — whether this evidence's direction agrees or
  conflicts with the Market Context Engine's current trend label for the
  symbol (see ``app/context/engine.py``).
- **cross-indicator confirmation** — how many other active pieces of
  evidence for the symbol agree in direction.
- **contradictory evidence** — a penalty when active evidence for the
  symbol takes the opposite directional stance.
- **correlation between evidence sources** — a simple, honestly-labeled
  proxy (1/sqrt(n) dampening for evidence sharing the same category),
  *not* a real statistical correlation calculation. True cross-source
  correlation is future work.
- **future machine learning adjustments** — an explicit no-op extension
  seam (``ml_adjustment``, always ``1.0`` today) so a later model can plug
  in a learned multiplier without changing this function's shape.

Weight formula (every factor multiplies around a neutral baseline, then
the product is clamped to ``[0.0, 1.0]``)::

    weight = source_reliability
           * freshness_factor
           * persistence_factor
           * timeframe_alignment_factor
           * cross_confirmation_factor
           * contradiction_factor
           * regime_alignment_factor
           * correlation_dampening_factor
           * ml_adjustment
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.aggregation.models import EnrichmentInfo
from app.event_bus.events import WeightedEvidenceEvent
from app.evidence.schema import Evidence

#: Applied to any evidence source with no explicit entry in
#: ``confidence_weighting.source_reliability`` — a neutral-leaning-honest
#: default (not full trust, not distrust) for a source the operator
#: hasn't tuned yet.
DEFAULT_SOURCE_RELIABILITY = 0.75

_ALIGNED_TREND = {"Bull Trend": "bullish", "Bear Trend": "bearish"}


@dataclass
class ConfidenceWeightingConfig:
    """Tunable inputs for the framework — see
    ``settings.confidence_weighting`` / ``config/default.yaml``. Nothing
    here is hardcoded per-source in code; operators tune weights through
    configuration, consistent with the project's "configuration over
    code" development requirement."""

    source_reliability: dict[str, float] = field(default_factory=dict)
    max_cross_confirmation_boost: float = 0.5  # up to +50% at 5+ confirming peers
    max_timeframe_alignment_boost: float = 0.2  # up to +20% when every peer shares the timeframe
    contradiction_penalty: float = 0.5  # multiply by this when directly contradicted
    regime_aligned_boost: float = 1.2
    regime_opposed_penalty: float = 0.85

    @classmethod
    def from_settings(cls, settings: Any) -> "ConfidenceWeightingConfig":
        section = getattr(settings, "confidence_weighting", None)
        if section is None:
            return cls()
        return cls(
            source_reliability=dict(getattr(section, "source_reliability", None) or {}),
            max_cross_confirmation_boost=getattr(section, "max_cross_confirmation_boost", 0.5),
            max_timeframe_alignment_boost=getattr(section, "max_timeframe_alignment_boost", 0.2),
            contradiction_penalty=getattr(section, "contradiction_penalty", 0.5),
            regime_aligned_boost=getattr(section, "regime_aligned_boost", 1.2),
            regime_opposed_penalty=getattr(section, "regime_opposed_penalty", 0.85),
        )


def compute_weight(
    evidence: Evidence,
    enrichment: EnrichmentInfo,
    active_evidence: list[Evidence],
    *,
    trend_label: str | None,
    config: ConfidenceWeightingConfig,
) -> WeightedEvidenceEvent:
    """Compute a normalized ``[0, 1]`` confidence weight for one piece of
    evidence plus a fully transparent breakdown of every factor that went
    into it.

    ``active_evidence`` is the aggregator's current fresh/deduped snapshot
    for the same symbol (used for cross-confirmation, contradiction,
    timeframe alignment, and correlation dampening). ``trend_label`` is
    the Market Context Engine's current trend label for this symbol
    (``"Bull Trend"`` / ``"Bear Trend"`` / anything else / ``None``), used
    for regime alignment — unknown or non-trend context leaves that factor
    neutral rather than guessing.
    """

    breakdown: dict[str, float] = {}
    peers = [e for e in active_evidence if e.evidence_id != evidence.evidence_id]

    source_reliability = config.source_reliability.get(evidence.source, DEFAULT_SOURCE_RELIABILITY)
    breakdown["source_reliability"] = round(source_reliability, 4)

    # Freshness: floor at 0.1 so evidence that's still fresh enough to be
    # "active" at all is never weighted all the way to zero purely by age.
    freshness_factor = 0.1 + 0.9 * max(0.0, min(1.0, enrichment.freshness))
    breakdown["freshness"] = round(freshness_factor, 4)

    # Persistence: a recurring finding is more trustworthy than a one-off,
    # with diminishing returns (occurrence 1 -> 0.5x floor, occurrence 5+
    # -> ~1.0x).
    persistence_factor = 0.5 + 0.5 * min(1.0, max(0, enrichment.occurrence_count - 1) / 4)
    breakdown["persistence"] = round(persistence_factor, 4)

    # Timeframe alignment: what fraction of the other active evidence for
    # this symbol shares this evidence's timeframe.
    if evidence.timeframe and peers:
        same_timeframe = sum(1 for e in peers if e.timeframe == evidence.timeframe)
        timeframe_alignment_factor = 1.0 + config.max_timeframe_alignment_boost * (same_timeframe / len(peers))
    else:
        timeframe_alignment_factor = 1.0
    breakdown["timeframe_alignment"] = round(timeframe_alignment_factor, 4)

    # Cross-indicator confirmation: how many other active pieces of
    # evidence agree in direction, capped so 5+ confirmations already
    # gets the full boost.
    confirming_peers = sum(1 for e in peers if e.direction == evidence.direction)
    confirmation_factor = 1.0 + config.max_cross_confirmation_boost * min(1.0, confirming_peers / 5)
    breakdown["cross_confirmation"] = round(confirmation_factor, 4)

    # Contradictory evidence: a penalty when active evidence for the
    # symbol takes the opposite directional stance (bullish vs bearish
    # only — neutral evidence never "contradicts").
    contradicted = any(
        e.direction != evidence.direction and {e.direction, evidence.direction} == {"bullish", "bearish"}
        for e in peers
    )
    contradiction_factor = config.contradiction_penalty if contradicted else 1.0
    breakdown["contradiction"] = contradiction_factor

    # Market regime alignment: trend-following evidence gets a boost when
    # it agrees with the current trend context, a penalty when it fights
    # it, and stays neutral (1.0) when context is unknown or non-trending.
    aligned_direction = _ALIGNED_TREND.get(trend_label or "")
    if aligned_direction is None:
        regime_factor = 1.0
    elif evidence.direction == aligned_direction:
        regime_factor = config.regime_aligned_boost
    elif evidence.direction in ("bullish", "bearish"):
        regime_factor = config.regime_opposed_penalty
    else:
        regime_factor = 1.0
    breakdown["regime_alignment"] = regime_factor

    # Correlation dampening: a simple, honestly-labeled proxy for "these
    # signals aren't independent" -- each additional active piece of
    # evidence sharing this one's category dampens the weight by
    # 1/sqrt(n), so several near-duplicate signals from the same
    # analytical family don't count as that many independent
    # confirmations. NOT a real statistical correlation -- see module
    # docstring.
    same_category_count = sum(1 for e in active_evidence if e.category == evidence.category)
    correlation_factor = (1.0 / (same_category_count**0.5)) if same_category_count > 1 else 1.0
    breakdown["correlation_dampening"] = round(correlation_factor, 4)

    # Future ML adjustments: explicit no-op extension seam. A later model
    # could return a learned multiplier here instead of the constant 1.0
    # without changing this function's shape or any caller.
    ml_adjustment = 1.0
    breakdown["ml_adjustment"] = ml_adjustment

    raw_weight = (
        source_reliability
        * freshness_factor
        * persistence_factor
        * timeframe_alignment_factor
        * confirmation_factor
        * contradiction_factor
        * regime_factor
        * correlation_factor
        * ml_adjustment
    )
    final_weight = max(0.0, min(1.0, raw_weight))
    breakdown["final_weight"] = round(final_weight, 4)

    return WeightedEvidenceEvent(evidence=evidence, weight=round(final_weight, 4), breakdown=breakdown)
