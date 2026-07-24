"""
The Portfolio Intelligence Layer.

Not a simple watchlist — a continuously-updated intelligence profile per
configured symbol (``settings.portfolio.watchlist``), synthesizing current
technical evidence, external intelligence, market context, confidence
trends, strategy matches, and historical alert state into one transparent
``priority_score`` (see ``app/portfolio/scoring.py``). This is what shifts
the assistant from reactive ("tell me about NVDA when I ask") to
proactive ("NVDA just became the most interesting thing on my watchlist,
and here's exactly why").

Inputs, purely through the Event Bus:

- ``EvidenceAggregated`` — technical evidence (indicator plugins) and
  fundamental/external intelligence evidence (News/Earnings/Macro
  plugins) arrive on the exact same event; the Confidence Weighting
  Framework's ``weighted_evidence`` gives this engine a ready-made
  strength signal without recomputing anything.
- ``MarketContextUpdated`` — the Market Context Engine's current labels
  per symbol.
- ``StrategyMatched`` — declarative strategy matches.
- ``AlertGenerated`` — the Event Prioritization Engine's own output,
  consumed here purely to track *historical alert state* (when a symbol
  was last alerted on, how many times) — this engine never decides
  whether to alert; that's the Prioritization Engine's job, one layer
  downstream. No cycle: this engine never publishes anything in direct,
  synchronous response to an alert, only updates its own cached state.

Output: ``SymbolProfileUpdated``, edge-triggered on a meaningful score
change (a small score jitter every tick would defeat the entire point of
"reduce notification fatigue" one layer downstream). This engine never
calls the Evidence Aggregator, Strategy Engine, Reasoning Engine, or the
Event Prioritization Engine directly — only through the Event Bus.

This engine only ever tracks symbols in ``settings.portfolio.watchlist`` —
a symbol outside it is never profiled, keeping memory bounded and
"which symbols are being watched" a pure configuration question.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.event_bus.bus import EventBus
from app.event_bus.events import AlertGenerated, EvidenceAggregated, MarketContextUpdated, StrategyMatched, SymbolProfileUpdated
from app.logging import get_logger
from app.portfolio.models import SymbolProfile
from app.portfolio.scoring import PortfolioScoringConfig, compute_priority

log = get_logger(__name__)

#: Evidence categories treated as "external intelligence" rather than
#: technical analysis -- matches the categories the reference News/
#: Earnings/Macro plugins publish (see plugins/intelligence/).
_FUNDAMENTAL_CATEGORIES = {"News", "Earnings", "Macro"}

#: Bounded so a long-running deployment's matched-strategy list per symbol
#: never grows without limit -- the most recent few are what's actually
#: useful to show on a watchlist.
_MAX_MATCHED_STRATEGIES = 5

#: Minimum priority-score delta before republishing SymbolProfileUpdated --
#: without this, a symbol sitting near a threshold would spam a fresh
#: event on essentially every tick.
_SCORE_CHANGE_THRESHOLD = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Tracked:
    profile: SymbolProfile
    weight_history: "deque[tuple[datetime, float]]" = field(default_factory=lambda: deque(maxlen=8))


class PortfolioIntelligenceEngine:
    """Maintains one :class:`~app.portfolio.models.SymbolProfile` per
    configured watchlist symbol and ranks them by priority. Attach once at
    bootstrap; ``/watchlist`` and `/analyze`'s portfolio line both query
    it on demand via ``snapshot()``/``ranked_watchlist()``."""

    def __init__(self, settings: Any) -> None:
        section = getattr(settings, "portfolio", None)
        self._watchlist: tuple[str, ...] = tuple(getattr(section, "watchlist", None) or [])
        self._trend_window = int(getattr(section, "confidence_trend_window", 8))
        self._trend_margin = float(getattr(section, "confidence_trend_margin", 0.05))
        self._fundamental_freshness_seconds = float(getattr(section, "fundamental_freshness_seconds", 600.0))
        self._scoring_config = PortfolioScoringConfig.from_settings(settings)

        self._tracked: dict[str, _Tracked] = {
            symbol: _Tracked(profile=SymbolProfile(symbol=symbol), weight_history=deque(maxlen=self._trend_window))
            for symbol in self._watchlist
        }
        self._last_fundamental_at: dict[str, datetime] = {}
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(EvidenceAggregated, self._on_evidence_aggregated, name="portfolio_intelligence_evidence")
        event_bus.subscribe(MarketContextUpdated, self._on_context_updated, name="portfolio_intelligence_context")
        event_bus.subscribe(StrategyMatched, self._on_strategy_matched, name="portfolio_intelligence_strategy")
        event_bus.subscribe(AlertGenerated, self._on_alert_generated, name="portfolio_intelligence_alerts")
        log.info("portfolio_intelligence_engine_attached", watchlist=list(self._watchlist))

    # ---------------------------------------------------------------- queries

    @property
    def watchlist(self) -> tuple[str, ...]:
        return self._watchlist

    def snapshot(self, symbol: str) -> SymbolProfile | None:
        """The current profile for ``symbol``, or ``None`` if it isn't on
        the configured watchlist."""
        tracked = self._tracked.get(symbol)
        return tracked.profile.model_copy(deep=True) if tracked else None

    def ranked_watchlist(self) -> list[SymbolProfile]:
        """Every tracked symbol's profile, highest priority first."""
        profiles = [t.profile.model_copy(deep=True) for t in self._tracked.values()]
        return sorted(profiles, key=lambda p: p.priority_score, reverse=True)

    # ---------------------------------------------------------------- handlers

    async def _on_evidence_aggregated(self, event: EvidenceAggregated) -> None:
        tracked = self._tracked.get(event.symbol)
        if tracked is None:
            return

        active = event.active_evidence
        weighted = event.weighted_evidence
        now = _utcnow()

        if event.evidence.category in _FUNDAMENTAL_CATEGORIES:
            self._last_fundamental_at[event.symbol] = now

        top_weight = max((w.weight for w in weighted), default=0.0)
        avg_weight = (sum(w.weight for w in weighted) / len(weighted)) if weighted else 0.0
        tracked.weight_history.append((now, avg_weight))

        profile = tracked.profile
        profile.active_evidence_count = len(active)
        profile.bullish_count = sum(1 for e in active if e.direction == "bullish")
        profile.bearish_count = sum(1 for e in active if e.direction == "bearish")
        profile.neutral_count = sum(1 for e in active if e.direction == "neutral")
        profile.top_weight = round(top_weight, 4)
        profile.avg_weight = round(avg_weight, 4)

        last_fundamental = self._last_fundamental_at.get(event.symbol)
        profile.has_fundamental_evidence = last_fundamental is not None
        profile.fundamental_evidence_fresh = bool(
            last_fundamental is not None
            and (now - last_fundamental).total_seconds() <= self._fundamental_freshness_seconds
        )
        profile.confidence_trend = self._compute_trend(tracked.weight_history)

        await self._recompute_and_maybe_publish(event.symbol)

    async def _on_context_updated(self, event: MarketContextUpdated) -> None:
        if event.symbol is None:
            return  # market-wide context isn't attributed to any one symbol's profile
        tracked = self._tracked.get(event.symbol)
        if tracked is None:
            return
        tracked.profile.context[event.context_type] = event.label
        await self._recompute_and_maybe_publish(event.symbol)

    async def _on_strategy_matched(self, event: StrategyMatched) -> None:
        tracked = self._tracked.get(event.symbol)
        if tracked is None:
            return
        names = tracked.profile.matched_strategies
        if event.strategy not in names:
            names.append(event.strategy)
            del names[: max(0, len(names) - _MAX_MATCHED_STRATEGIES)]
        await self._recompute_and_maybe_publish(event.symbol)

    async def _on_alert_generated(self, event: AlertGenerated) -> None:
        if event.symbol is None:
            return
        tracked = self._tracked.get(event.symbol)
        if tracked is None:
            return
        tracked.profile.last_alert_at = event.timestamp
        tracked.profile.alert_count += 1
        await self._recompute_and_maybe_publish(event.symbol)

    # ---------------------------------------------------------------- scoring

    def _compute_trend(self, history: "deque[tuple[datetime, float]]") -> str:
        if len(history) < 4:
            return "unknown"
        values = [weight for _, weight in history]
        mid = len(values) // 2
        older_avg = sum(values[:mid]) / mid
        recent_avg = sum(values[mid:]) / (len(values) - mid)
        delta = recent_avg - older_avg
        if delta > self._trend_margin:
            return "rising"
        if delta < -self._trend_margin:
            return "falling"
        return "stable"

    async def _recompute_and_maybe_publish(self, symbol: str) -> None:
        tracked = self._tracked[symbol]
        profile = tracked.profile
        now = _utcnow()

        score, breakdown = compute_priority(
            top_weight=profile.top_weight,
            has_fresh_fundamental_evidence=profile.fundamental_evidence_fresh,
            context=profile.context,
            confidence_trend=profile.confidence_trend,
            matched_strategies=profile.matched_strategies,
            last_alert_at=profile.last_alert_at,
            now=now,
            config=self._scoring_config,
        )
        changed = abs(score - profile.priority_score) >= _SCORE_CHANGE_THRESHOLD
        profile.priority_score = score
        profile.priority_breakdown = breakdown
        profile.updated_at = now

        if not changed or self._event_bus is None:
            return

        await self._event_bus.publish(
            SymbolProfileUpdated(
                source="PortfolioIntelligenceEngine",
                symbol=symbol,
                priority_score=score,
                priority_breakdown=breakdown,
                bullish_count=profile.bullish_count,
                bearish_count=profile.bearish_count,
                neutral_count=profile.neutral_count,
                top_weight=profile.top_weight,
                confidence_trend=profile.confidence_trend,
                context=dict(profile.context),
                matched_strategies=list(profile.matched_strategies),
                last_alert_at=profile.last_alert_at,
                alert_count=profile.alert_count,
            )
        )
        log.debug("symbol_profile_updated", symbol=symbol, priority_score=score)
