"""Unit tests for the Reflection Engine (``app/reflection/engine.py``) --
the automatic post-decision analysis generator introduced in Milestone 10.
"""
from __future__ import annotations

from app.core.clock import SimulatedClock
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, ReflectionGenerated, SymbolProfileUpdated
from app.reflection.engine import ReflectionEngine


def _decision(**overrides) -> DecisionRecorded:
    defaults = dict(
        source="SimulationEngine",
        symbol="NVDA",
        market_context={"trend": "Bull Trend"},
        technical_evidence=[
            "EMA: Bullish EMA Cross (bullish, 80/100)",
            "VWAP: Price Crossed Below VWAP (bearish, 60/100)",
        ],
        fundamental_evidence=["News: NVDA beats estimates (bullish, 70/100)"],
        reasoning_summary="Bullish evidence dominates.",
        reasoning_source="evidence_only",
        confidence=71.5,
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        bar_index=5,
        lookahead_bars=10,
        outcome="correct",
        outcome_price_change_pct=1.5,
        outcome_pending=False,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


async def test_reflection_generated_for_a_resolved_decision(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    captured: list[ReflectionGenerated] = []

    async def _capture(event: ReflectionGenerated) -> None:
        captured.append(event)

    event_bus.subscribe(ReflectionGenerated, _capture)
    await event_bus.publish(_decision())
    await event_bus.drain()

    # Published on the real bus -- not just recorded internally.
    assert len(captured) == 1

    assert engine.total_generated == 1
    records = engine.for_symbol("NVDA")
    assert len(records) == 1
    record = records[0]
    assert record.symbol == "NVDA"
    assert record.outcome == "correct"
    assert record.reasoning == "Bullish evidence dominates."
    assert "EMA: Bullish EMA Cross (bullish, 80/100)" in record.supporting_evidence
    assert "VWAP: Price Crossed Below VWAP (bearish, 60/100)" in record.contradictory_evidence
    assert record.lessons_learned
    assert record.potential_improvements


async def test_reflection_skipped_while_outcome_pending(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(_decision(outcome=None, outcome_pending=True))
    await event_bus.drain()

    assert engine.total_generated == 0
    assert engine.for_symbol("NVDA") == []


async def test_no_action_decision_has_no_supporting_or_contradictory_evidence(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(
        _decision(
            simulated_action="no_action",
            outcome=None,
            outcome_pending=False,
            technical_evidence=[],
            fundamental_evidence=[],
        )
    )
    await event_bus.drain()

    record = engine.for_symbol("NVDA")[0]
    assert record.supporting_evidence == []
    assert record.contradictory_evidence == []
    assert "insufficient evidence" in record.lessons_learned.lower()


async def test_incorrect_outcome_produces_distinct_lessons_from_correct(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(_decision(outcome="correct"))
    await event_bus.publish(_decision(outcome="incorrect", outcome_price_change_pct=-1.5))
    await event_bus.drain()

    records = engine.for_symbol("NVDA")
    assert len(records) == 2
    correct_record, incorrect_record = records
    assert correct_record.lessons_learned != incorrect_record.lessons_learned
    assert "aligned with" in correct_record.lessons_learned
    assert "did not hold" in incorrect_record.lessons_learned


async def test_neutral_outcome_produces_a_neutral_band_lesson(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(_decision(outcome="neutral", outcome_price_change_pct=0.1))
    await event_bus.drain()

    record = engine.for_symbol("NVDA")[0]
    assert "neutral band" in record.lessons_learned.lower()
    assert "neutral_band_pct" in record.potential_improvements


async def test_outcome_none_for_directional_action_still_produces_a_lesson(settings, event_bus: EventBus):
    """A directional decision can resolve with outcome=None if no entry
    price existed at decision time -- distinct from no_action (which has
    no direction at all)."""
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(_decision(outcome=None, outcome_pending=False, price_at_decision=None))
    await event_bus.drain()

    record = engine.for_symbol("NVDA")[0]
    assert "could not be" in record.lessons_learned


async def test_confidence_trend_is_cached_from_symbol_profile_updated(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(SymbolProfileUpdated(source="test", symbol="NVDA", priority_score=50.0, confidence_trend="rising"))
    await event_bus.drain()
    await event_bus.publish(_decision())
    await event_bus.drain()

    record = engine.for_symbol("NVDA")[0]
    assert record.confidence_evolution == "rising"


async def test_confidence_trend_defaults_to_unknown_when_never_cached(settings, event_bus: EventBus):
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(_decision(symbol="AAPL"))
    await event_bus.drain()

    record = engine.for_symbol("AAPL")[0]
    assert record.confidence_evolution == "unknown"


async def test_disabled_reflection_engine_never_generates_or_publishes(settings, event_bus: EventBus):
    settings.reflection.enabled = False
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    published: list[ReflectionGenerated] = []

    async def _capture(event: ReflectionGenerated) -> None:
        published.append(event)

    event_bus.subscribe(ReflectionGenerated, _capture)
    await event_bus.publish(_decision())
    await event_bus.drain()

    assert engine.total_generated == 0
    assert published == []


async def test_history_is_bounded_per_symbol(settings, event_bus: EventBus):
    settings.reflection.history_max_per_symbol = 3
    engine = ReflectionEngine(settings)
    engine.attach(event_bus)

    for i in range(5):
        await event_bus.publish(_decision(bar_index=i))
    await event_bus.drain()

    records = engine.for_symbol("NVDA")
    assert len(records) == 3  # oldest evicted, bounded deque
    assert engine.total_generated == 5  # the running total is never reset by eviction


async def test_clock_injection_controls_reflection_timestamp(settings, event_bus: EventBus):
    clock = SimulatedClock()
    engine = ReflectionEngine(settings, clock=clock)
    engine.attach(event_bus)

    await event_bus.publish(_decision())
    await event_bus.drain()

    record = engine.for_symbol("NVDA")[0]
    assert record.timestamp == clock.now()


def test_symbols_and_all_queries(settings):
    import asyncio

    async def _run():
        event_bus = EventBus.from_settings(settings)
        engine = ReflectionEngine(settings)
        engine.attach(event_bus)
        await event_bus.publish(_decision(symbol="NVDA"))
        await event_bus.publish(_decision(symbol="AAPL"))
        await event_bus.drain()

        assert engine.symbols() == ["AAPL", "NVDA"]
        assert len(engine.all()) == 2
        await event_bus.shutdown()

    asyncio.run(_run())
