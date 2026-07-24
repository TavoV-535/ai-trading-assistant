"""
Event Prioritization scoring.

Turns one candidate development (fresh evidence, a strategy match, a
market context shift) into a transparent ``[0, 100]`` alert score,
considering the factors PROJECT.md's Milestone 8 spec calls out:

- **importance** — how significant this development is intrinsically.
  Source-specific: a strategy match starts from a high base (a curated,
  cross-indicator confirmation); a context shift's base depends on
  whether its ``context_type`` is inherently high-stakes (Gap Day,
  Risk-On/Off, macro events) or more routine (trend/volatility drift);
  raw evidence's base scales with the Confidence Weighting Framework's
  own weight for it — the two systems compose rather than duplicate each
  other's work.
- **novelty** — is this a genuinely new development or a repeat? For
  evidence, ``1 / occurrence_count`` (a first sighting is fully novel; a
  fifth repeat of the same finding contributes a fifth as much). Strategy
  matches and context shifts are already edge-triggered one layer
  upstream (only fire on a genuine transition), so they're always fully
  novel by the time they reach this engine.
- **confidence changes** — a bonus when the symbol's Portfolio
  Intelligence Layer confidence trend (``app/portfolio/engine.py``) is
  actively rising or falling; "stable" contributes nothing, since nothing
  changed.
- **urgency** — a source-specific ``[0, 1]`` time-sensitivity signal
  (Gap Day / macro events score high; a routine trend continuation scores
  low). For raw evidence, a simple, documented proxy on the plugin's own
  ``score`` magnitude — not a universal truth, since plugin score scales
  aren't standardized, but a defensible signal until a richer one exists.
- **user relevance** — a flat bonus when the symbol is on the configured
  Portfolio Intelligence Layer watchlist.

Duplicate suppression and the accept/reject threshold are cooldown/config
concerns handled by ``app/prioritization/engine.py``, not by this module —
this module only ever answers "how important is this candidate," never
"should we actually alert."
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PrioritizationScoringConfig:
    """Point budgets for each factor (0-100 total, clamped). These are
    structural constants rather than exploded into a dozen more config
    fields — ``settings.prioritization`` covers the values PROJECT.md
    calls out as operationally tunable (alert threshold, cooldown,
    watchlist-only) in ``app/prioritization/engine.py``."""

    strategy_match_importance: float = 40.0
    evidence_importance_scale: float = 35.0
    context_high_importance: float = 35.0
    context_low_importance: float = 15.0
    novelty_points: float = 20.0
    confidence_rising_points: float = 15.0
    confidence_falling_points: float = 10.0
    urgency_points: float = 15.0
    user_relevance_points: float = 20.0


def compute_alert_score(
    *,
    base_importance: float,
    novelty: float,
    confidence_trend: str,
    urgency: float,
    in_watchlist: bool,
    config: PrioritizationScoringConfig,
) -> tuple[float, dict[str, float]]:
    """Compute one candidate's alert score plus a transparent breakdown.

    ``novelty`` and ``urgency`` are both ``[0, 1]`` inputs — the caller
    (``app/prioritization/engine.py``) is responsible for deriving them
    per source event type; this function only ever combines already-
    computed factors.
    """

    breakdown: dict[str, float] = {"importance": round(base_importance, 2)}

    novelty_points = max(0.0, min(1.0, novelty)) * config.novelty_points
    breakdown["novelty"] = round(novelty_points, 2)

    if confidence_trend == "rising":
        change_points = config.confidence_rising_points
    elif confidence_trend == "falling":
        change_points = config.confidence_falling_points
    else:
        change_points = 0.0
    breakdown["confidence_change"] = change_points

    urgency_points = max(0.0, min(1.0, urgency)) * config.urgency_points
    breakdown["urgency"] = round(urgency_points, 2)

    relevance_points = config.user_relevance_points if in_watchlist else 0.0
    breakdown["user_relevance"] = relevance_points

    total = base_importance + novelty_points + change_points + urgency_points + relevance_points
    final_score = max(0.0, min(100.0, total))
    breakdown["final_score"] = round(final_score, 2)
    return round(final_score, 2), breakdown


def urgency_label(score: float) -> str:
    """Maps a final alert score to a human ``AlertGenerated.urgency``
    label. Only ever called for scores that already cleared the alert
    threshold, so "low" is never actually produced today — kept as a
    documented, reachable value for a future lower-threshold use case."""
    if score >= 90:
        return "critical"
    if score >= 75:
        return "high"
    if score >= 40:
        return "normal"
    return "low"
