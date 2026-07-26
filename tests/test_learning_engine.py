"""Unit tests for the Learning Engine (``app/learning/engine.py``) —
Milestone 12's centerpiece. Uses real (not mocked) Strategy Analytics,
Evidence Reliability, Confidence Calibration, and Knowledge Graph Query
collaborators attached to the same bus, exactly like production wiring —
"no duplicated calculations" means the engine never recomputes what those
services already own, so tests exercise it through real data rather than
stubbing every collaborator method."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analytics.calibration import ConfidenceCalibrationService
from app.analytics.evidence_reliability import EvidenceReliabilityEngine
from app.analytics.strategy_analytics import StrategyAnalyticsService
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, JournalCreated, RiskEvent
from app.knowledge_graph.graph import KnowledgeGraph
from app.knowledge_graph.query import KnowledgeGraphQueryEngine
from app.learning.engine import LearningEngine


def _decision(*, bar_index: int, timestamp: datetime, **overrides) -> DecisionRecorded:
    defaults = dict(
        source="test",
        symbol="NVDA",
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
        fundamental_evidence=[],
        strategy_matches=["Momentum Breakout"],
        market_context={"volatility": "High Volatility"},
        confidence=70.0,
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        bar_index=bar_index,
        lookahead_bars=5,
        outcome="correct",
        outcome_price_change_pct=1.0,
        outcome_pending=False,
        timestamp=timestamp,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


def _build_engine(settings, bus: EventBus, *, min_sample=2, review_interval=2, overtrading_per_day=10.0, streak_length=2):
    settings.learning.min_sample_size = min_sample
    settings.learning.review_interval_decisions = review_interval
    settings.learning.overtrading_decisions_per_day = overtrading_per_day
    settings.learning.streak_length = streak_length

    strategy_analytics = StrategyAnalyticsService(settings)
    evidence_reliability = EvidenceReliabilityEngine(settings)
    confidence_calibration = ConfidenceCalibrationService(settings, min_sample=1)
    graph = KnowledgeGraph(settings)
    kg_query = KnowledgeGraphQueryEngine(graph, evidence_reliability=evidence_reliability, confidence_calibration=confidence_calibration, min_sample=min_sample)

    strategy_analytics.attach(bus)
    evidence_reliability.attach(bus)
    confidence_calibration.attach(bus)
    graph.attach(bus)

    engine = LearningEngine(
        settings,
        strategy_analytics=strategy_analytics,
        evidence_reliability=evidence_reliability,
        confidence_calibration=confidence_calibration,
        knowledge_graph_query=kg_query,
    )
    engine.attach(bus)
    return engine


async def test_review_cadence_triggers_automatically_every_n_resolved_decisions(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus, review_interval=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    assert engine.statistics()["total_reviews"] == 0
    await event_bus.publish(_decision(bar_index=0, timestamp=base))
    await event_bus.drain()
    assert engine.statistics()["total_reviews"] == 0  # only 1 of 2 resolved so far

    await event_bus.publish(_decision(bar_index=1, timestamp=base + timedelta(minutes=1)))
    await event_bus.drain()
    assert engine.statistics()["total_reviews"] == 1  # 2nd resolved decision triggers a review
    await event_bus.shutdown()


async def test_outcome_pending_decisions_never_count_toward_the_review_cadence(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus, review_interval=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await event_bus.publish(_decision(bar_index=0, timestamp=base, outcome=None, outcome_pending=True))
    await event_bus.publish(_decision(bar_index=1, timestamp=base, outcome=None, outcome_pending=True))
    await event_bus.drain()
    assert engine.statistics()["total_reviews"] == 0
    await event_bus.shutdown()


async def test_review_is_directly_callable_and_publishes_to_the_bus(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus, min_sample=2, review_interval=100)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    captured = []

    async def _capture(event):
        captured.append(event)

    from app.event_bus.events import CoachingEvent
    event_bus.subscribe(CoachingEvent, _capture)

    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i)))
    await event_bus.drain()

    events = await engine.review()
    await event_bus.drain()

    assert isinstance(events, list)
    assert len(events) > 0
    assert len(captured) == len(events)  # every returned event was actually published
    assert engine.recent_coaching_events() == events or set(e.event_id for e in events) <= {e.event_id for e in engine.recent_coaching_events()}
    await event_bus.shutdown()


async def test_strongest_and_weakest_strategy_detectors(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus, min_sample=2, review_interval=100)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    # "Winner" strategy: all correct. "Loser" strategy: all incorrect.
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i), strategy_matches=["Winner"], outcome="correct"))
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i), strategy_matches=["Loser"], outcome="incorrect"))
    await event_bus.drain()

    events = await engine.review()
    by_pattern = {e.pattern_type: e for e in events}

    assert "strongest_strategy" in by_pattern
    assert by_pattern["strongest_strategy"].related_strategies == ["Winner"]
    assert "weakest_strategy" in by_pattern
    assert by_pattern["weakest_strategy"].related_strategies == ["Loser"]
    assert by_pattern["weakest_strategy"].priority == "high"  # win_rate 0.0 < 0.4
    await event_bus.shutdown()


async def test_overtrading_detector_triggers_on_high_decision_frequency(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus, min_sample=2, review_interval=100, overtrading_per_day=1.0)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)

    # Several decisions within minutes of each other -> far more than 1/day.
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i)))
    await event_bus.drain()

    events = await engine.review()
    assert any(e.pattern_type == "overtrading" for e in events)
    await event_bus.shutdown()


async def test_recurring_mistake_detector_finds_repeated_evidence_before_losing_streaks(settings, event_bus: EventBus):
    """The Knowledge Graph Query Layer's streak walk recognizes a streak
    the moment ``run`` first reaches ``streak_length`` and attributes it to
    the evidence on the decision immediately *before* the streak started —
    so each streak here is: one "prior" decision carrying the repeated
    evidence line, followed by exactly ``streak_length`` incorrect
    decisions. A single correct decision between the two streaks resets
    the run counter (and doubles as the second streak's own "prior")."""
    engine = _build_engine(settings, event_bus, min_sample=2, review_interval=100, streak_length=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    mistake_evidence = ["Earnings: EPS miss (bearish, 60/100)"]

    decisions = [
        _decision(bar_index=0, timestamp=base, technical_evidence=mistake_evidence, outcome="correct"),
        _decision(bar_index=1, timestamp=base + timedelta(minutes=1), outcome="incorrect"),
        _decision(bar_index=2, timestamp=base + timedelta(minutes=2), outcome="incorrect"),  # streak 1 recognized here
        _decision(bar_index=3, timestamp=base + timedelta(minutes=3), technical_evidence=mistake_evidence, outcome="correct"),  # resets run, doubles as streak 2's "prior"
        _decision(bar_index=4, timestamp=base + timedelta(minutes=4), outcome="incorrect"),
        _decision(bar_index=5, timestamp=base + timedelta(minutes=5), outcome="incorrect"),  # streak 2 recognized here
    ]
    for d in decisions:
        await event_bus.publish(d)
    await event_bus.drain()

    events = await engine.review()
    mistake_events = [e for e in events if e.pattern_type == "recurring_mistake"]
    assert mistake_events
    assert mistake_events[0].historical_frequency == 2  # both streaks attributed to the same evidence
    assert "Earnings: EPS miss" in mistake_events[0].summary
    await event_bus.shutdown()


async def test_journal_and_risk_events_are_tracked_without_altering_history(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus)
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="Felt good about this one."))
    await event_bus.publish(RiskEvent(source="test", symbol="NVDA", risk_type="daily_drawdown", severity="warning", value=1.0, profile_name="test", message="warn"))
    await event_bus.drain()

    diagnostics = engine.diagnostics()
    assert diagnostics["journal_notes_tracked"] == 1
    assert diagnostics["risk_events_tracked"] == 1
    await event_bus.shutdown()


async def test_recent_coaching_events_respects_limit(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus, min_sample=2, review_interval=100)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i)))
    await event_bus.drain()
    await engine.review()

    all_events = engine.recent_coaching_events()
    limited = engine.recent_coaching_events(limit=1)
    assert len(limited) == 1
    if all_events:
        assert limited[0].event_id == all_events[-1].event_id
    await event_bus.shutdown()


async def test_health_diagnostics_statistics_shapes(settings, event_bus: EventBus):
    engine = _build_engine(settings, event_bus)
    health = await engine.health()
    assert health["status"] == "healthy"
    assert health["enabled"] is True

    diagnostics = engine.diagnostics()
    assert "review_interval_decisions" in diagnostics

    stats = engine.statistics()
    assert "total_reviews" in stats and "coaching_events_published" in stats
    await event_bus.shutdown()


async def test_a_broken_detector_never_crashes_the_whole_review(settings, event_bus: EventBus, monkeypatch):
    engine = _build_engine(settings, event_bus, min_sample=2, review_interval=100)

    def _boom():
        raise RuntimeError("synthetic detector failure")

    monkeypatch.setattr(engine, "_detect_strongest_strategy", _boom)

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i)))
    await event_bus.drain()

    # Must not raise, and other detectors still run.
    events = await engine.review()
    assert isinstance(events, list)
    await event_bus.shutdown()
