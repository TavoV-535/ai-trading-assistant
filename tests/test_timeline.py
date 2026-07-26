from __future__ import annotations

import asyncio

from app.event_bus import DecisionRecorded, EventBus
from app.timeline import DecisionRecord, DecisionTimeline


def _decision(symbol: str, bar_index: int, **overrides) -> DecisionRecorded:
    defaults = dict(
        source="SimulationEngine",
        symbol=symbol,
        market_context={"trend": "Bull Trend"},
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
        fundamental_evidence=[],
        confidence_weights={"EMA:Bullish EMA Cross": 0.6},
        strategy_matches=["Momentum Breakout"],
        reasoning_summary="Bullish evidence dominates.",
        reasoning_source="evidence_only",
        confidence=70.0,
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        bar_index=bar_index,
        lookahead_bars=10,
        outcome=None,
        outcome_price_change_pct=None,
        outcome_pending=True,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


async def test_decision_timeline_attach_subscribes_to_decision_recorded(settings, event_bus: EventBus):
    timeline = DecisionTimeline(settings)
    timeline.attach(event_bus)

    await event_bus.publish(_decision("NVDA", bar_index=0))
    await asyncio.sleep(0.05)

    assert timeline.total_recorded == 1
    records = timeline.for_symbol("NVDA")
    assert len(records) == 1
    assert isinstance(records[0], DecisionRecord)
    assert records[0].symbol == "NVDA"
    assert records[0].strategy_matches == ["Momentum Breakout"]
    await event_bus.shutdown()


async def test_decision_timeline_ignores_other_symbols(settings, event_bus: EventBus):
    timeline = DecisionTimeline(settings)
    timeline.attach(event_bus)

    await event_bus.publish(_decision("NVDA", bar_index=0))
    await event_bus.publish(_decision("AAPL", bar_index=0))
    await asyncio.sleep(0.05)

    assert [r.symbol for r in timeline.for_symbol("NVDA")] == ["NVDA"]
    assert [r.symbol for r in timeline.for_symbol("AAPL")] == ["AAPL"]
    assert timeline.for_symbol("TSLA") == []
    assert timeline.symbols() == ["AAPL", "NVDA"]
    await event_bus.shutdown()


async def test_decision_timeline_for_symbol_preserves_order_oldest_first(settings, event_bus: EventBus):
    timeline = DecisionTimeline(settings)
    timeline.attach(event_bus)

    for bar_index in (0, 5, 10):
        await event_bus.publish(_decision("NVDA", bar_index=bar_index))
    await asyncio.sleep(0.05)

    records = timeline.for_symbol("NVDA")
    assert [r.bar_index for r in records] == [0, 5, 10]
    await event_bus.shutdown()


async def test_decision_timeline_for_symbol_limit_returns_most_recent(settings, event_bus: EventBus):
    timeline = DecisionTimeline(settings)
    timeline.attach(event_bus)

    for bar_index in (0, 5, 10, 15):
        await event_bus.publish(_decision("NVDA", bar_index=bar_index))
    await asyncio.sleep(0.05)

    records = timeline.for_symbol("NVDA", limit=2)
    assert [r.bar_index for r in records] == [10, 15]
    await event_bus.shutdown()


async def test_decision_timeline_all_sorts_by_timestamp_across_symbols(settings, event_bus: EventBus):
    timeline = DecisionTimeline(settings)
    timeline.attach(event_bus)

    from datetime import datetime, timedelta, timezone

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await event_bus.publish(_decision("NVDA", bar_index=0, timestamp=base + timedelta(minutes=2)))
    await event_bus.publish(_decision("AAPL", bar_index=0, timestamp=base))
    await asyncio.sleep(0.05)

    all_records = timeline.all()
    assert [r.symbol for r in all_records] == ["AAPL", "NVDA"]
    await event_bus.shutdown()


async def test_decision_timeline_bounded_per_symbol(settings, event_bus: EventBus):
    timeline = DecisionTimeline(settings, max_per_symbol=3)
    timeline.attach(event_bus)

    for bar_index in range(5):
        await event_bus.publish(_decision("NVDA", bar_index=bar_index))
    await asyncio.sleep(0.05)

    records = timeline.for_symbol("NVDA")
    assert len(records) == 3
    # Oldest entries evicted first -- deque(maxlen=...) semantics.
    assert [r.bar_index for r in records] == [2, 3, 4]
    # total_recorded keeps counting every decision ever seen, even evicted
    # ones -- the durable database record (EventLogRepository.decision_records())
    # is the unbounded source of truth; this in-memory view is bounded on purpose.
    assert timeline.total_recorded == 5
    await event_bus.shutdown()


def test_decision_record_from_event_round_trips_every_field():
    event = _decision(
        "NVDA",
        bar_index=5,
        outcome="correct",
        outcome_price_change_pct=1.5,
        outcome_pending=False,
    )
    record = DecisionRecord.from_event(event)

    assert record.event_id == event.event_id
    assert record.symbol == event.symbol
    assert record.market_context == event.market_context
    assert record.technical_evidence == event.technical_evidence
    assert record.confidence_weights == event.confidence_weights
    assert record.strategy_matches == event.strategy_matches
    assert record.reasoning_summary == event.reasoning_summary
    assert record.simulated_action == event.simulated_action
    assert record.outcome == "correct"
    assert record.outcome_price_change_pct == 1.5
    assert record.outcome_pending is False
