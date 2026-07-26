"""
The Learning Engine.

Per PROJECT.md's Milestone 12 spec: "The Learning Engine should
continuously analyze historical Decision Timeline records, Trading
Journal entries, Reflection events, Capital Protection events, Strategy
Matches, Market Context, Intelligence Events, and Performance metrics to
identify recurring trading behaviors and publish immutable Coaching
Events... The Learning Engine should never alter historical data. It
should only observe, reason, and publish new events."

Not a plugin — a core service, the same tier as the Decision Timeline.
Recognizes *long-term behavioral patterns*, not individual trades:
"continuously analyze" is implemented as re-running a full pattern review
every ``learning.review_interval_decisions`` newly-*resolved* decisions
(mirroring the Reflection Engine's "reflect once ``outcome_pending``
flips to ``False``" trigger), rather than on every single event — a real,
testable, deterministic cadence, not a vague background loop.

Every pattern this engine reports is a plain, deterministic rule over
already-computed statistics: composes ``StrategyAnalyticsService``,
``EvidenceReliabilityEngine``, ``ConfidenceCalibrationService``, and
``KnowledgeGraphQueryEngine`` (each owns its own calculation — "no
duplicated calculations") for the patterns those services are best suited
for, and maintains a small bounded history of its own only for patterns
nothing else already tracks (time-of-day/day-of-week/session
performance, overtrading/undertrading frequency, journal-note keyword
trend). No machine learning anywhere in this module.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from app.analytics.calibration import ConfidenceCalibrationService
from app.analytics.evidence_reliability import EvidenceReliabilityEngine
from app.analytics.strategy_analytics import StrategyAnalyticsService
from app.core.clock import Clock, SystemClock
from app.event_bus.bus import EventBus
from app.event_bus.events import CoachingEvent, DecisionRecorded, JournalCreated, RiskEvent
from app.knowledge_graph.query import KnowledgeGraphQueryEngine
from app.logging import get_logger

log = get_logger(__name__)

_DEFAULT_REVIEW_INTERVAL = 5
_DEFAULT_MIN_SAMPLE = 5


class _LightDecision:
    __slots__ = ("symbol", "timestamp", "confidence", "outcome", "outcome_pending")

    def __init__(self, symbol: str, timestamp: datetime, confidence: float, outcome: str | None, outcome_pending: bool) -> None:
        self.symbol = symbol
        self.timestamp = timestamp
        self.confidence = confidence
        self.outcome = outcome
        self.outcome_pending = outcome_pending


class LearningEngine:
    """Observes the platform's own history and publishes immutable
    ``CoachingEvent``s. Attach once at bootstrap (or once per Simulation
    Engine run); every consumer (``/coach``, a future Dashboard) reads
    published ``CoachingEvent``s off the bus, or calls :meth:`review`
    directly for an on-demand analysis (what ``/coach`` does)."""

    def __init__(
        self,
        settings: Any,
        *,
        strategy_analytics: StrategyAnalyticsService,
        evidence_reliability: EvidenceReliabilityEngine,
        confidence_calibration: ConfidenceCalibrationService,
        knowledge_graph_query: KnowledgeGraphQueryEngine,
        clock: Clock | None = None,
    ) -> None:
        section = getattr(settings, "learning", None)
        self._enabled = bool(getattr(section, "enabled", True))
        self._review_interval = max(1, int(getattr(section, "review_interval_decisions", _DEFAULT_REVIEW_INTERVAL)))
        self._min_sample = max(1, int(getattr(section, "min_sample_size", _DEFAULT_MIN_SAMPLE)))
        self._streak_length = max(2, int(getattr(section, "streak_length", 2)))
        self._overtrading_per_day = float(getattr(section, "overtrading_decisions_per_day", 10.0))
        self._undertrading_per_day = float(getattr(section, "undertrading_decisions_per_day", 0.5))
        self._positive_keywords = [k.lower() for k in (getattr(section, "positive_journal_keywords", None) or [])]
        self._negative_keywords = [k.lower() for k in (getattr(section, "negative_journal_keywords", None) or [])]
        self._session_ranges: dict[str, list[int]] = dict(getattr(section, "session_hour_ranges", None) or {})
        max_decisions = int(getattr(section, "history_max_decisions", 2000))
        max_notes = int(getattr(section, "history_max_journal_notes", 500))
        max_risk = int(getattr(section, "history_max_risk_events", 2000))

        self._strategy_analytics = strategy_analytics
        self._evidence_reliability = evidence_reliability
        self._confidence_calibration = confidence_calibration
        self._kg_query = knowledge_graph_query
        self._clock: Clock = clock or SystemClock()

        self._decisions: "deque[_LightDecision]" = deque(maxlen=max_decisions)
        self._journal_notes: "deque[tuple[datetime, str]]" = deque(maxlen=max_notes)
        self._risk_severities: "deque[tuple[datetime, str]]" = deque(maxlen=max_risk)
        self._resolved_since_review = 0
        self._total_reviews = 0
        self._coaching_events: "deque[CoachingEvent]" = deque(maxlen=200)
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="learning_engine_decisions")
        event_bus.subscribe(JournalCreated, self._on_journal_created, name="learning_engine_journal")
        event_bus.subscribe(RiskEvent, self._on_risk_event, name="learning_engine_risk")
        log.info("learning_engine_attached", enabled=self._enabled, review_interval_decisions=self._review_interval)

    # ---------------------------------------------------------------- event handlers

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        self._decisions.append(_LightDecision(event.symbol, event.timestamp, event.confidence, event.outcome, event.outcome_pending))
        if self._enabled and not event.outcome_pending and event.outcome in ("correct", "incorrect", "neutral"):
            self._resolved_since_review += 1
            if self._resolved_since_review >= self._review_interval:
                self._resolved_since_review = 0
                await self.review()

    async def _on_journal_created(self, event: JournalCreated) -> None:
        if event.note:
            self._journal_notes.append((event.timestamp, event.note))

    async def _on_risk_event(self, event: RiskEvent) -> None:
        self._risk_severities.append((event.timestamp, event.severity))

    # ---------------------------------------------------------------- review / detectors

    async def review(self) -> list[CoachingEvent]:
        """Runs every pattern detector once and publishes whatever
        ``CoachingEvent``s cross their trigger threshold. Safe to call
        directly (``/coach`` does, for an on-demand analysis) as well as
        being triggered automatically every ``review_interval_decisions``
        resolved decisions."""
        self._total_reviews += 1
        detectors = [
            self._detect_strongest_strategy,
            self._detect_weakest_strategy,
            self._detect_strongest_evidence_combination,
            self._detect_weakest_evidence_combination,
            self._detect_best_market_context,
            self._detect_worst_market_context,
            self._detect_confidence_calibration,
            self._detect_recurring_mistake,
            self._detect_recurring_strength,
            self._detect_overtrading,
            self._detect_undertrading,
            self._detect_risk_management_habit,
            self._detect_stop_loss_behavior,
            self._detect_profit_taking_behavior,
            self._detect_hold_time_trend,
            self._detect_volatility_regime_performance,
            self._detect_time_of_day_performance,
            self._detect_day_of_week_performance,
            self._detect_market_session_performance,
            self._detect_watchlist_performance_trend,
            self._detect_emotional_trend,
        ]
        published: list[CoachingEvent] = []
        for detector in detectors:
            try:
                event = detector()
            except Exception:  # pragma: no cover - defensive; a bad detector must never crash review()
                log.exception("learning_engine_detector_error", detector=detector.__name__)
                continue
            if event is not None:
                self._coaching_events.append(event)
                published.append(event)
                if self._event_bus is not None:
                    await self._event_bus.publish(event)
        log.info("learning_engine_review_complete", detectors_triggered=len(published), total_reviews=self._total_reviews)
        return published

    def _make_event(self, *, pattern_type: str, title: str, **kwargs: Any) -> CoachingEvent:
        return CoachingEvent(source="LearningEngine", timestamp=self._clock.now(), pattern_type=pattern_type, title=title, **kwargs)

    # -- strategy performance -------------------------------------------------

    def _detect_strongest_strategy(self) -> CoachingEvent | None:
        stats = [s for s in self._strategy_analytics.all() if s.sample_size >= self._min_sample]
        if not stats:
            return None
        best = max(stats, key=lambda s: s.win_rate)
        return self._make_event(
            pattern_type="strongest_strategy",
            title=f"'{best.strategy}' is your strongest strategy",
            summary=f"'{best.strategy}' has a {best.win_rate:.0%} win rate over {best.sample_size} decision(s).",
            confidence=min(100.0, best.sample_size * 5.0),
            related_strategies=[best.strategy],
            historical_frequency=best.sample_size,
            priority="medium",
            suggested_improvements=["Consider whether position sizing on this strategy has room to grow within your Risk Profile's limits."],
        )

    def _detect_weakest_strategy(self) -> CoachingEvent | None:
        stats = [s for s in self._strategy_analytics.all() if s.sample_size >= self._min_sample]
        if not stats:
            return None
        worst = min(stats, key=lambda s: s.win_rate)
        return self._make_event(
            pattern_type="weakest_strategy",
            title=f"'{worst.strategy}' is your weakest strategy",
            summary=f"'{worst.strategy}' has only a {worst.win_rate:.0%} win rate over {worst.sample_size} decision(s).",
            confidence=min(100.0, worst.sample_size * 5.0),
            related_strategies=[worst.strategy],
            historical_frequency=worst.sample_size,
            priority="high" if worst.win_rate < 0.4 else "medium",
            suggested_improvements=[f"Review recent '{worst.strategy}' decisions for a common contradicting evidence pattern before its next signal."],
        )

    # -- evidence combinations --------------------------------------------------

    def _detect_strongest_evidence_combination(self) -> CoachingEvent | None:
        result = self._kg_query.strongest_evidence_combinations()
        if not result.supporting_data:
            return None
        combo = result.supporting_data.get("combination", [])
        return self._make_event(
            pattern_type="strongest_evidence_combination",
            title="Strongest-performing evidence combination",
            summary=result.answer,
            evidence=result.explanation,
            confidence=float(result.supporting_data.get("win_rate", 0.0)) * 100,
            priority="low",
            suggested_improvements=[f"Watch for '{' + '.join(combo)}' appearing together again."] if combo else [],
        )

    def _detect_weakest_evidence_combination(self) -> CoachingEvent | None:
        result = self._kg_query.weakest_evidence_combinations()
        if not result.supporting_data:
            return None
        combo = result.supporting_data.get("combination", [])
        return self._make_event(
            pattern_type="weakest_evidence_combination",
            title="Weakest-performing evidence combination",
            summary=result.answer,
            evidence=result.explanation,
            confidence=float(result.supporting_data.get("win_rate", 0.0)) * 100,
            priority="medium",
            suggested_improvements=[f"Treat '{' + '.join(combo)}' together as a lower-confidence setup." if combo else "Review this evidence combination's history before acting on it again."],
        )

    # -- market context ----------------------------------------------------

    def _detect_best_market_context(self) -> CoachingEvent | None:
        result = self._kg_query.best_market_regimes()
        if not result.supporting_data:
            return None
        return self._make_event(
            pattern_type="best_market_context",
            title="Best-performing market regime",
            summary=result.answer,
            evidence=result.explanation,
            related_market_contexts=[result.supporting_data.get("regime", "")],
            confidence=float(result.supporting_data.get("win_rate", 0.0)) * 100,
            priority="low",
        )

    def _detect_worst_market_context(self) -> CoachingEvent | None:
        result = self._kg_query.worst_market_regimes()
        if not result.supporting_data:
            return None
        return self._make_event(
            pattern_type="worst_market_context",
            title="Worst-performing market regime",
            summary=result.answer,
            evidence=result.explanation,
            related_market_contexts=[result.supporting_data.get("regime", "")],
            confidence=float(result.supporting_data.get("win_rate", 0.0)) * 100,
            priority="high",
            suggested_improvements=["Consider reducing position size or sitting out when this regime is active."],
        )

    # -- calibration ----------------------------------------------------

    def _detect_confidence_calibration(self) -> CoachingEvent | None:
        report = self._confidence_calibration.report()
        scored = [b for b in report.buckets if b.verdict not in ("insufficient_data", "well_calibrated")]
        if not scored:
            return None
        worst = max(scored, key=lambda b: abs(b.actual_win_rate - b.expected_rate))
        return self._make_event(
            pattern_type="confidence_calibration",
            title=f"Confidence is {worst.verdict} in the {worst.label} range",
            summary=report.overall_verdict,
            evidence=[f"{b.label}: expected ~{b.expected_rate:.0%}, actual {b.actual_win_rate:.0%} ({b.sample_size} decisions)." for b in report.buckets if b.sample_size],
            confidence=min(100.0, worst.sample_size * 10.0),
            priority="medium",
            suggested_improvements=[f"Treat {worst.label} confidence decisions with extra scrutiny -- they haven't matched their implied win rate."],
        )

    # -- recurring patterns ----------------------------------------------------

    def _detect_recurring_mistake(self) -> CoachingEvent | None:
        result = self._kg_query.recurring_mistakes_before_losing_streaks(streak_length=self._streak_length)
        if not result.supporting_data or not result.supporting_data.get("evidence_counts"):
            return None
        return self._make_event(
            pattern_type="recurring_mistake",
            title="Recurring mistake before losing streaks",
            summary=result.answer,
            evidence=result.explanation,
            historical_frequency=int(result.supporting_data.get("streaks_found", 0)),
            confidence=70.0,
            priority="high",
            suggested_improvements=["Pause and double-check this evidence pattern before acting when it appears again."],
        )

    def _detect_recurring_strength(self) -> CoachingEvent | None:
        result = self._kg_query.recurring_strengths_before_winning_streaks(streak_length=self._streak_length)
        if not result.supporting_data or not result.supporting_data.get("evidence_counts"):
            return None
        return self._make_event(
            pattern_type="recurring_strength",
            title="Recurring strength before winning streaks",
            summary=result.answer,
            evidence=result.explanation,
            historical_frequency=int(result.supporting_data.get("streaks_found", 0)),
            confidence=70.0,
            priority="low",
        )

    # -- trading frequency ----------------------------------------------------

    def _decisions_per_day(self) -> float:
        if len(self._decisions) < 2:
            return 0.0
        ordered = sorted(self._decisions, key=lambda d: d.timestamp)
        span_days = max((ordered[-1].timestamp - ordered[0].timestamp).total_seconds() / 86400.0, 1.0 / 24)
        return len(ordered) / span_days

    def _detect_overtrading(self) -> CoachingEvent | None:
        if len(self._decisions) < self._min_sample:
            return None
        rate = self._decisions_per_day()
        if rate <= self._overtrading_per_day:
            return None
        return self._make_event(
            pattern_type="overtrading",
            title="Overtrading detected",
            summary=f"Averaging {rate:.1f} decisions/day, above the {self._overtrading_per_day:.1f}/day overtrading threshold.",
            confidence=60.0,
            priority="medium",
            suggested_improvements=["Consider raising the evidence bar or lengthening the decision interval before acting again."],
        )

    def _detect_undertrading(self) -> CoachingEvent | None:
        if len(self._decisions) < self._min_sample:
            return None
        rate = self._decisions_per_day()
        if rate >= self._undertrading_per_day or rate <= 0:
            return None
        return self._make_event(
            pattern_type="undertrading",
            title="Undertrading detected",
            summary=f"Averaging only {rate:.2f} decisions/day, below the {self._undertrading_per_day:.2f}/day undertrading threshold.",
            confidence=50.0,
            priority="low",
        )

    # -- risk-management habits ----------------------------------------------------

    def _detect_risk_management_habit(self) -> CoachingEvent | None:
        if len(self._risk_severities) < self._min_sample:
            return None
        ordered = sorted(self._risk_severities, key=lambda r: r[0])
        mid = len(ordered) // 2
        first_bad = sum(1 for _, sev in ordered[:mid] if sev in ("warning", "critical")) / max(mid, 1)
        second_bad = sum(1 for _, sev in ordered[mid:] if sev in ("warning", "critical")) / max(len(ordered) - mid, 1)
        if abs(second_bad - first_bad) < 0.1:
            return None
        trend = "worsening" if second_bad > first_bad else "improving"
        return self._make_event(
            pattern_type="risk_management_habit",
            title=f"Risk-management habits are {trend}",
            summary=f"Warning/critical risk evaluations were {first_bad:.0%} of the first half of observed history vs. {second_bad:.0%} of the second half.",
            confidence=55.0,
            priority="high" if trend == "worsening" else "low",
            suggested_improvements=["Review the active Risk Profile's thresholds; consider a more conservative profile."] if trend == "worsening" else [],
        )

    def _detect_stop_loss_behavior(self) -> CoachingEvent | None:
        stats = [s for s in self._strategy_analytics.all() if s.max_drawdown is not None and s.sample_size >= self._min_sample]
        if not stats:
            return None
        worst = min(stats, key=lambda s: s.max_drawdown if s.max_drawdown is not None else 0.0)
        if worst.max_drawdown is None or worst.expectancy is None or worst.expectancy == 0:
            return None
        ratio = abs(worst.max_drawdown) / abs(worst.expectancy * worst.sample_size) if worst.expectancy else 0.0
        if ratio < 1.5:
            return None
        return self._make_event(
            pattern_type="stop_loss_behavior",
            title=f"'{worst.strategy}' shows undisciplined stop-loss behavior",
            summary=f"'{worst.strategy}'s worst drawdown ({worst.max_drawdown:.2f}) is disproportionate to its typical per-trade result.",
            confidence=50.0,
            related_strategies=[worst.strategy],
            priority="high",
            suggested_improvements=["Consider a tighter stop-loss or smaller position size for this strategy."],
        )

    def _detect_profit_taking_behavior(self) -> CoachingEvent | None:
        stats = [s for s in self._strategy_analytics.all() if s.profit_factor is not None and s.sample_size >= self._min_sample]
        candidates = [s for s in stats if s.win_rate > 0.5 and s.profit_factor is not None and s.profit_factor < 1.0]
        if not candidates:
            return None
        worst = min(candidates, key=lambda s: s.profit_factor or 0.0)
        return self._make_event(
            pattern_type="profit_taking_behavior",
            title=f"'{worst.strategy}' may be cutting winners short",
            summary=f"'{worst.strategy}' wins {worst.win_rate:.0%} of the time but its profit factor is only {worst.profit_factor:.2f} -- losses are outweighing wins in size.",
            confidence=50.0,
            related_strategies=[worst.strategy],
            priority="medium",
            suggested_improvements=["Consider letting winning trades run longer, or tightening losing trades sooner."],
        )

    def _detect_hold_time_trend(self) -> CoachingEvent | None:
        stats = [s for s in self._strategy_analytics.all() if s.average_hold_time_bars is not None]
        if not stats:
            return None
        # Report on the strategy with the most sample data, purely as an
        # observation -- shorter/longer hold time isn't inherently
        # good or bad, so this pattern never asserts a verdict.
        best = max(stats, key=lambda s: s.sample_size)
        return self._make_event(
            pattern_type="hold_time_trend",
            title=f"Average hold time for '{best.strategy}'",
            summary=f"'{best.strategy}' decisions look ahead an average of {best.average_hold_time_bars:.1f} bars.",
            confidence=40.0,
            related_strategies=[best.strategy],
            priority="low",
        )

    def _detect_volatility_regime_performance(self) -> CoachingEvent | None:
        buckets: dict[str, list[bool]] = defaultdict(list)
        for s in self._strategy_analytics.all():
            for label, rate in s.volatility_performance.items():
                buckets[label].append(rate)
        if not buckets:
            return None
        averaged = {label: sum(rates) / len(rates) for label, rates in buckets.items()}
        best_label = max(averaged, key=lambda k: averaged[k])
        worst_label = min(averaged, key=lambda k: averaged[k])
        return self._make_event(
            pattern_type="volatility_regime_performance",
            title="Volatility regime performance",
            summary=f"Win rate is {averaged[best_label]:.0%} during '{best_label}' vs. {averaged[worst_label]:.0%} during '{worst_label}'.",
            related_market_contexts=[best_label, worst_label],
            confidence=45.0,
            priority="low",
        )

    def _bucketed_win_rate(self, key_fn: Any) -> dict[str, list[_LightDecision]]:
        buckets: dict[str, list[_LightDecision]] = defaultdict(list)
        for d in self._decisions:
            if d.outcome_pending or d.outcome not in ("correct", "incorrect"):
                continue
            buckets[key_fn(d)].append(d)
        return buckets

    @staticmethod
    def _win_rate_of(decisions: list[_LightDecision]) -> float:
        if not decisions:
            return 0.0
        return sum(1 for d in decisions if d.outcome == "correct") / len(decisions)

    def _detect_time_of_day_performance(self) -> CoachingEvent | None:
        buckets = self._bucketed_win_rate(lambda d: f"{(d.timestamp.hour // 4) * 4:02d}:00-{((d.timestamp.hour // 4) * 4 + 4) % 24:02d}:00 UTC")
        eligible = {k: v for k, v in buckets.items() if len(v) >= self._min_sample}
        if len(eligible) < 2:
            return None
        rates = {k: self._win_rate_of(v) for k, v in eligible.items()}
        best = max(rates, key=lambda k: rates[k])
        worst = min(rates, key=lambda k: rates[k])
        return self._make_event(
            pattern_type="time_of_day_performance",
            title="Time-of-day performance",
            summary=f"Best window: {best} ({rates[best]:.0%} win rate). Worst window: {worst} ({rates[worst]:.0%}).",
            confidence=45.0,
            priority="low",
        )

    def _detect_day_of_week_performance(self) -> CoachingEvent | None:
        buckets = self._bucketed_win_rate(lambda d: d.timestamp.strftime("%A"))
        eligible = {k: v for k, v in buckets.items() if len(v) >= self._min_sample}
        if len(eligible) < 2:
            return None
        rates = {k: self._win_rate_of(v) for k, v in eligible.items()}
        best = max(rates, key=lambda k: rates[k])
        worst = min(rates, key=lambda k: rates[k])
        return self._make_event(
            pattern_type="day_of_week_performance",
            title="Day-of-week performance",
            summary=f"Best day: {best} ({rates[best]:.0%} win rate). Worst day: {worst} ({rates[worst]:.0%}).",
            confidence=45.0,
            priority="low",
        )

    def _session_for_hour(self, hour: int) -> str:
        for label, bounds in self._session_ranges.items():
            lo, hi = bounds[0], bounds[1]
            if lo <= hour < hi:
                return label
        return "Unclassified"

    def _detect_market_session_performance(self) -> CoachingEvent | None:
        buckets = self._bucketed_win_rate(lambda d: self._session_for_hour(d.timestamp.hour))
        eligible = {k: v for k, v in buckets.items() if len(v) >= self._min_sample}
        if len(eligible) < 2:
            return None
        rates = {k: self._win_rate_of(v) for k, v in eligible.items()}
        best = max(rates, key=lambda k: rates[k])
        worst = min(rates, key=lambda k: rates[k])
        return self._make_event(
            pattern_type="market_session_performance",
            title="Market session performance",
            summary=f"Best session: {best} ({rates[best]:.0%} win rate). Worst session: {worst} ({rates[worst]:.0%}). (Approximate UTC-hour-based sessions -- no real exchange calendar integration exists yet.)",
            confidence=40.0,
            priority="low",
        )

    def _detect_watchlist_performance_trend(self) -> CoachingEvent | None:
        resolved = [d for d in self._decisions if not d.outcome_pending and d.outcome in ("correct", "incorrect")]
        if len(resolved) < self._min_sample * 2:
            return None
        ordered = sorted(resolved, key=lambda d: d.timestamp)
        mid = len(ordered) // 2
        first_rate = self._win_rate_of(ordered[:mid])
        second_rate = self._win_rate_of(ordered[mid:])
        if abs(second_rate - first_rate) < 0.05:
            trend = "stable"
        elif second_rate > first_rate:
            trend = "improving"
        else:
            trend = "declining"
        return self._make_event(
            pattern_type="watchlist_performance_trend",
            title=f"Overall watchlist performance is {trend}",
            summary=f"Win rate across the whole watchlist moved from {first_rate:.0%} to {second_rate:.0%} over the observed history.",
            confidence=50.0,
            priority="high" if trend == "declining" else "low",
        )

    def _detect_emotional_trend(self) -> CoachingEvent | None:
        if len(self._journal_notes) < self._min_sample or not (self._positive_keywords or self._negative_keywords):
            return None
        ordered = sorted(self._journal_notes, key=lambda n: n[0])
        mid = len(ordered) // 2

        def _score(notes: list[tuple[datetime, str]]) -> int:
            score = 0
            for _, text in notes:
                lowered = text.lower()
                score += sum(1 for kw in self._positive_keywords if kw in lowered)
                score -= sum(1 for kw in self._negative_keywords if kw in lowered)
            return score

        first_score = _score(ordered[:mid])
        second_score = _score(ordered[mid:])
        if first_score == second_score:
            return None
        trend = "improving" if second_score > first_score else "declining"
        return self._make_event(
            pattern_type="emotional_trend",
            title=f"Journal tone is {trend}",
            summary=(
                f"Keyword-based tone score moved from {first_score} to {second_score} across your journal notes "
                "(a simple keyword heuristic, not real sentiment analysis)."
            ),
            confidence=30.0,
            priority="medium" if trend == "declining" else "low",
        )

    # ---------------------------------------------------------------- queries

    def recent_coaching_events(self, *, limit: int | None = None) -> list[CoachingEvent]:
        events = list(self._coaching_events)
        return events[-limit:] if limit is not None and limit < len(events) else events

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy" if self._enabled else "degraded", "enabled": self._enabled, "total_reviews": self._total_reviews}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "decisions_tracked": len(self._decisions),
            "journal_notes_tracked": len(self._journal_notes),
            "risk_events_tracked": len(self._risk_severities),
            "resolved_since_last_review": self._resolved_since_review,
            "review_interval_decisions": self._review_interval,
            "coaching_events_published": len(self._coaching_events),
        }

    def statistics(self) -> dict[str, Any]:
        return {
            "total_reviews": self._total_reviews,
            "coaching_events_published": len(self._coaching_events),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
