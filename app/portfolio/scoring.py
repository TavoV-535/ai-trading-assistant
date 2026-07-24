"""
Portfolio priority scoring.

Turns a watchlist symbol's raw, already-computed signals into one
transparent ``[0, 100]`` "how much attention does this symbol deserve
right now" score — the number ``/watchlist`` ranks by. Every factor is
documented and independently testable; the returned ``breakdown`` dict
keeps each one visible, the same explainability convention the Confidence
Weighting Framework established (``app/aggregation/weighting.py``).

Factors, per PROJECT.md's Milestone 8 spec:

- **current technical evidence** — ``top_weight``, the strongest currently
  active piece of evidence's Confidence Weighting Framework weight.
- **external intelligence** — a bonus when fresh News/Earnings/Macro
  evidence has arrived recently (``has_fresh_fundamental_evidence``).
- **market context** — a bonus per "notable" active context label
  (Bull/Bear Trend, High/Low Volatility, Gap Day, Trend Exhaustion,
  Risk-On/Risk-Off, macro events), capped so a symbol with many context
  labels doesn't dominate purely on label count.
- **confidence trends** — a bonus when the symbol's average evidence
  weight is trending up *or* down (a genuine change is worth surfacing;
  "stable" contributes nothing).
- **strategy matches** — a flat bonus when at least one declarative
  strategy is currently matched for the symbol.
- **historical alert state** — a dampening factor (not a hard cutoff) when
  the symbol was already alerted on recently, so the watchlist doesn't
  keep re-surfacing the same already-alerted development at the top.

Point budgets are configurable via ``PortfolioScoringConfig`` /
``settings.portfolio`` for the values PROJECT.md calls out explicitly
(notable context types, alert-suppression window/factor); the per-factor
point budgets themselves are structural constants, documented here rather
than exploded into a dozen more config fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_NOTABLE_CONTEXT_TYPES: tuple[str, ...] = (
    "trend",
    "volatility",
    "gap",
    "exhaustion",
    "risk_regime",
    "macro_event",
)


@dataclass
class PortfolioScoringConfig:
    notable_context_types: tuple[str, ...] = DEFAULT_NOTABLE_CONTEXT_TYPES
    alert_suppression_seconds: float = 300.0
    alert_suppression_factor: float = 0.5

    # -- point budgets (0-100 total before alert-suppression dampening) --
    evidence_strength_points: float = 30.0
    fundamental_freshness_points: float = 15.0
    context_points_per_label: float = 5.0
    context_points_cap: float = 20.0
    confidence_rising_points: float = 15.0
    confidence_falling_points: float = 10.0
    strategy_match_points: float = 20.0

    @classmethod
    def from_settings(cls, settings: Any) -> "PortfolioScoringConfig":
        section = getattr(settings, "portfolio", None)
        if section is None:
            return cls()
        return cls(
            notable_context_types=tuple(getattr(section, "notable_context_types", None) or DEFAULT_NOTABLE_CONTEXT_TYPES),
            alert_suppression_seconds=float(getattr(section, "alert_suppression_seconds", 300.0)),
            alert_suppression_factor=float(getattr(section, "alert_suppression_factor", 0.5)),
        )


def compute_priority(
    *,
    top_weight: float,
    has_fresh_fundamental_evidence: bool,
    context: dict[str, str],
    confidence_trend: str,
    matched_strategies: list[str],
    last_alert_at: datetime | None,
    now: datetime,
    config: PortfolioScoringConfig,
) -> tuple[float, dict[str, float]]:
    """Compute one symbol's priority score plus a transparent breakdown."""

    breakdown: dict[str, float] = {}

    evidence_strength = min(1.0, max(0.0, top_weight)) * config.evidence_strength_points
    breakdown["evidence_strength"] = round(evidence_strength, 2)

    fundamental_freshness = config.fundamental_freshness_points if has_fresh_fundamental_evidence else 0.0
    breakdown["fundamental_freshness"] = fundamental_freshness

    notable_count = sum(1 for ctype in context if ctype in config.notable_context_types)
    context_intensity = min(config.context_points_cap, notable_count * config.context_points_per_label)
    breakdown["context_intensity"] = context_intensity

    if confidence_trend == "rising":
        trend_points = config.confidence_rising_points
    elif confidence_trend == "falling":
        trend_points = config.confidence_falling_points
    else:
        trend_points = 0.0
    breakdown["confidence_trend"] = trend_points

    strategy_points = config.strategy_match_points if matched_strategies else 0.0
    breakdown["strategy_match"] = strategy_points

    raw_total = evidence_strength + fundamental_freshness + context_intensity + trend_points + strategy_points

    suppression_factor = 1.0
    if last_alert_at is not None:
        age_seconds = (now - last_alert_at).total_seconds()
        if age_seconds < config.alert_suppression_seconds:
            suppression_factor = config.alert_suppression_factor
    breakdown["alert_suppression_factor"] = suppression_factor

    final_score = max(0.0, min(100.0, raw_total * suppression_factor))
    breakdown["final_score"] = round(final_score, 2)
    return round(final_score, 2), breakdown
