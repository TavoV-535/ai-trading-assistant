"""
The Event Prioritization Engine.

Sits between the Evidence Aggregator (and the Strategy/Market Context
Engines) and user notifications. Every candidate development —
newly-aggregated evidence, a strategy match, a market context shift — is
scored (``app/prioritization/scoring.py``) and either becomes a real
``AlertGenerated`` event (which ``app/discord/bot.py`` delivers to a
configured channel) or is silently suppressed, with the reason always
recorded for transparency (``decision_history()``).

This is what keeps the platform from spamming a Discord channel every
time an indicator ticks: only genuinely significant, novel, relevant
developments clear ``prioritization.alert_threshold``, and even those are
subject to a per-``(symbol, alert_key)`` cooldown so the same finding
re-firing doesn't re-alert every time.

Inputs, purely through the Event Bus:

- ``EvidenceAggregated`` — candidate: the newly-arrived evidence itself.
- ``StrategyMatched`` — candidate: the strategy match.
- ``MarketContextUpdated`` — candidate: the context shift.
- ``SymbolProfileUpdated`` (the Portfolio Intelligence Layer's output) —
  consumed purely to cache each symbol's current confidence trend for the
  "confidence changes" scoring factor. This engine never calls the
  Portfolio Intelligence Layer directly, and never republishes anything
  in direct response to a profile update — no cycle (see
  ``app/portfolio/engine.py``'s docstring for the other half of this).

The configured watchlist (``settings.portfolio.watchlist``) is read
directly from settings, the same static configuration the Portfolio
Intelligence Layer reads — not learned by waiting for that engine's
events, which would leave a symbol unable to alert until its first
profile change.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.event_bus.bus import EventBus
from app.event_bus.events import AlertGenerated, EvidenceAggregated, MarketContextUpdated, StrategyMatched, SymbolProfileUpdated
from app.logging import get_logger
from app.prioritization.scoring import PrioritizationScoringConfig, compute_alert_score, urgency_label

log = get_logger(__name__)

#: MarketContextUpdated context_types treated as inherently high-stakes —
#: everything else gets the "routine" importance base instead.
_HIGH_IMPORTANCE_CONTEXT_TYPES = {"risk_regime", "macro_event", "gap"}

#: A magnitude-based urgency proxy for raw evidence needs *some* scale to
#: normalize a plugin's own ``score`` against -- not a universal truth
#: (plugin score scales aren't standardized), a documented, defensible
#: default until a richer per-source calibration exists.
_EVIDENCE_URGENCY_SCALE = 30.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AlertDecision:
    """One transparency record — accepted or suppressed, always with a
    reason. Retained per symbol, bounded, queryable via
    ``EventPrioritizationEngine.decision_history()``."""

    accepted: bool
    symbol: str | None
    title: str
    score: float
    breakdown: dict[str, float]
    reason: str
    source_event_type: str
    decided_at: datetime


class EventPrioritizationEngine:
    """Evaluates candidate developments and decides which ones become
    real alerts. Attach once at bootstrap, after the Evidence Aggregator,
    Strategy Engine, Market Context Engine, and Portfolio Intelligence
    Layer — subscription order doesn't functionally matter (everything
    here is event-bus mediated), only that ``attach()`` runs before
    events start flowing."""

    def __init__(self, settings: Any) -> None:
        section = getattr(settings, "prioritization", None)
        self._alert_threshold = float(getattr(section, "alert_threshold", 60.0))
        self._cooldown_seconds = float(getattr(section, "alert_cooldown_seconds", 300.0))
        self._watchlist_only = bool(getattr(section, "watchlist_only", True))
        self._decision_log_size = int(getattr(section, "decision_log_size", 20))
        self._scoring_config = PrioritizationScoringConfig()

        portfolio_section = getattr(settings, "portfolio", None)
        self._watchlist: set[str] = set(getattr(portfolio_section, "watchlist", None) or [])

        self._confidence_trend: dict[str, str] = {}
        #: (symbol-or-"__market__", alert_key) -> last time an alert with
        #: that key actually fired -- the duplicate-suppression cooldown.
        self._last_alert_at: dict[tuple[str, str], datetime] = {}
        self._decisions: dict[str, "deque[AlertDecision]"] = defaultdict(lambda: deque(maxlen=self._decision_log_size))
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(EvidenceAggregated, self._on_evidence_aggregated, name="event_prioritization_evidence")
        event_bus.subscribe(StrategyMatched, self._on_strategy_matched, name="event_prioritization_strategy")
        event_bus.subscribe(MarketContextUpdated, self._on_context_updated, name="event_prioritization_context")
        event_bus.subscribe(SymbolProfileUpdated, self._on_profile_updated, name="event_prioritization_profile")
        log.info(
            "event_prioritization_engine_attached",
            alert_threshold=self._alert_threshold,
            watchlist_only=self._watchlist_only,
            watchlist=sorted(self._watchlist),
        )

    # ---------------------------------------------------------------- queries

    def decision_history(self, symbol: str | None) -> list[AlertDecision]:
        """The most recent accept/suppress decisions for ``symbol`` (or
        market-wide candidates, if ``symbol`` is ``None``) — transparency
        into why an alert did or didn't fire, without publishing every
        suppressed candidate as its own event (which would defeat the
        entire point of reducing notification fatigue)."""
        return list(self._decisions.get(symbol or "__market__", []))

    # ---------------------------------------------------------------- profile tracking

    async def _on_profile_updated(self, event: SymbolProfileUpdated) -> None:
        self._confidence_trend[event.symbol] = event.confidence_trend

    # ---------------------------------------------------------------- candidates

    async def _on_evidence_aggregated(self, event: EvidenceAggregated) -> None:
        weight = next(
            (w.weight for w in event.weighted_evidence if w.evidence.evidence_id == event.evidence.evidence_id),
            None,
        )
        if weight is None:
            weight = event.evidence.confidence / 100.0
        base_importance = weight * self._scoring_config.evidence_importance_scale

        occurrence_count = int(event.enrichment.get("occurrence_count", 1) or 1)
        novelty = 1.0 / max(1, occurrence_count)
        urgency = min(1.0, abs(event.evidence.score) / _EVIDENCE_URGENCY_SCALE)

        await self._evaluate(
            symbol=event.symbol,
            title=f"{event.evidence.source}: {event.evidence.title}",
            message=(
                f"{event.symbol}: {event.evidence.title} "
                f"({event.evidence.direction}, confidence {event.evidence.confidence:.0f}/100)."
            ),
            source_event_type="EvidenceAggregated",
            base_importance=base_importance,
            novelty=novelty,
            urgency=urgency,
            alert_key=f"evidence:{event.evidence.source}:{event.evidence.title}",
        )

    async def _on_strategy_matched(self, event: StrategyMatched) -> None:
        await self._evaluate(
            symbol=event.symbol,
            title=f"Strategy matched: {event.strategy}",
            message=f"{event.symbol}: '{event.strategy}' strategy matched (score {event.score}, {event.evidence_count} evidence).",
            source_event_type="StrategyMatched",
            base_importance=self._scoring_config.strategy_match_importance,
            novelty=1.0,  # already edge-triggered upstream -- always a genuinely new match
            urgency=0.6,
            alert_key=f"strategy:{event.strategy}",
        )

    async def _on_context_updated(self, event: MarketContextUpdated) -> None:
        high_importance = event.context_type in _HIGH_IMPORTANCE_CONTEXT_TYPES
        base_importance = (
            self._scoring_config.context_high_importance if high_importance else self._scoring_config.context_low_importance
        )
        subject = event.symbol or "the market"
        await self._evaluate(
            symbol=event.symbol,
            title=f"{event.context_type}: {event.label}",
            message=f"{subject}: market context shifted to {event.label}.",
            source_event_type="MarketContextUpdated",
            base_importance=base_importance,
            novelty=1.0,  # edge-triggered upstream too
            urgency=0.8 if high_importance else 0.3,
            alert_key=f"context:{event.context_type}",
        )

    # ---------------------------------------------------------------- shared decision path

    async def _evaluate(
        self,
        *,
        symbol: str | None,
        title: str,
        message: str,
        source_event_type: str,
        base_importance: float,
        novelty: float,
        urgency: float,
        alert_key: str,
    ) -> None:
        now = _utcnow()

        if self._watchlist_only and (symbol is None or symbol not in self._watchlist):
            self._record(symbol, title, 0.0, {}, "symbol not on the configured watchlist", source_event_type, now, accepted=False)
            return

        cooldown_key = (symbol or "__market__", alert_key)
        last_alert = self._last_alert_at.get(cooldown_key)
        if last_alert is not None and (now - last_alert).total_seconds() < self._cooldown_seconds:
            self._record(symbol, title, 0.0, {}, "duplicate suppressed (within cooldown)", source_event_type, now, accepted=False)
            return

        trend = self._confidence_trend.get(symbol, "unknown") if symbol else "unknown"
        in_watchlist = symbol in self._watchlist if symbol else False

        score, breakdown = compute_alert_score(
            base_importance=base_importance,
            novelty=novelty,
            confidence_trend=trend,
            urgency=urgency,
            in_watchlist=in_watchlist,
            config=self._scoring_config,
        )

        if score < self._alert_threshold:
            self._record(symbol, title, score, breakdown, "below alert threshold", source_event_type, now, accepted=False)
            return

        self._last_alert_at[cooldown_key] = now
        self._record(symbol, title, score, breakdown, "alert generated", source_event_type, now, accepted=True)

        if self._event_bus is None:
            return
        await self._event_bus.publish(
            AlertGenerated(
                source="EventPrioritizationEngine",
                symbol=symbol,
                title=title,
                message=message,
                score=score,
                breakdown=breakdown,
                reason=f"scored {score:.1f} (threshold {self._alert_threshold:.1f})",
                urgency=urgency_label(score),
                source_event_type=source_event_type,
            )
        )
        log.info("alert_generated", symbol=symbol, title=title, score=score, urgency=urgency_label(score))

    def _record(
        self,
        symbol: str | None,
        title: str,
        score: float,
        breakdown: dict[str, float],
        reason: str,
        source_event_type: str,
        decided_at: datetime,
        *,
        accepted: bool,
    ) -> None:
        key = symbol or "__market__"
        self._decisions[key].append(
            AlertDecision(
                accepted=accepted,
                symbol=symbol,
                title=title,
                score=score,
                breakdown=breakdown,
                reason=reason,
                source_event_type=source_event_type,
                decided_at=decided_at,
            )
        )
