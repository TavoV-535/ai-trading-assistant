"""Unit tests for the Memory Index (``app/memory/index.py``) —
Milestone 12."""
from __future__ import annotations

from app.event_bus.bus import EventBus
from app.event_bus.events import CoachingEvent, DecisionRecorded, JournalCreated, ReflectionGenerated
from app.memory.index import MemoryIndex


async def test_indexes_reflections_journal_notes_coaching_and_decisions(settings, event_bus: EventBus):
    index = MemoryIndex(settings)
    index.attach(event_bus)

    await event_bus.publish(ReflectionGenerated(source="test", symbol="NVDA", decision_event_id=__import__("uuid").uuid4(), reasoning="Bullish setup formed.", lessons_learned="Entry timing was good.", outcome="correct"))
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="Felt very confident."))
    await event_bus.publish(CoachingEvent(source="test", pattern_type="strongest_strategy", title="Momentum Breakout is strongest", summary="High win rate.", symbol="NVDA"))
    await event_bus.publish(DecisionRecorded(source="test", symbol="NVDA", reasoning_summary="Bullish evidence dominates.", confidence=70.0, simulated_action="watch_bullish", price_at_decision=100.0, bar_index=0, lookahead_bars=5, outcome="correct", outcome_pending=False))
    await event_bus.drain()

    assert index.statistics()["entries"] == 4
    await event_bus.shutdown()


async def test_decision_recorded_pending_outcome_is_not_indexed(settings, event_bus: EventBus):
    index = MemoryIndex(settings)
    index.attach(event_bus)
    await event_bus.publish(DecisionRecorded(source="test", symbol="NVDA", reasoning_summary="Pending decision.", confidence=70.0, simulated_action="watch_bullish", price_at_decision=100.0, bar_index=0, lookahead_bars=5, outcome=None, outcome_pending=True))
    await event_bus.drain()
    assert index.statistics()["entries"] == 0
    await event_bus.shutdown()


async def test_journal_created_with_no_note_is_not_indexed(settings, event_bus: EventBus):
    index = MemoryIndex(settings)
    index.attach(event_bus)
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note=None))
    await event_bus.drain()
    assert index.statistics()["entries"] == 0
    await event_bus.shutdown()


async def test_retrieve_ranks_by_keyword_overlap(settings, event_bus: EventBus):
    index = MemoryIndex(settings)
    index.attach(event_bus)

    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="Bullish momentum breakout confirmed by volume."))
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="Just a routine check, nothing notable."))
    await event_bus.publish(JournalCreated(source="test", symbol="AAPL", note="Bullish momentum breakout on AAPL too."))
    await event_bus.drain()

    results = index.retrieve("bullish momentum breakout", top_k=5)
    assert len(results) == 2
    assert all("momentum" in r.text.lower() for r in results)


async def test_retrieve_filters_by_symbol_and_tags(settings, event_bus: EventBus):
    index = MemoryIndex(settings)
    index.attach(event_bus)
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="Bullish momentum breakout."))
    await event_bus.publish(JournalCreated(source="test", symbol="AAPL", note="Bullish momentum breakout."))
    await event_bus.drain()

    nvda_only = index.retrieve("bullish momentum", symbol="NVDA")
    assert len(nvda_only) == 1
    assert nvda_only[0].symbol == "NVDA"

    tagged = index.retrieve("bullish momentum", tags=["journal_note"])
    assert len(tagged) == 2

    none_tagged = index.retrieve("bullish momentum", tags=["reflection"])
    assert none_tagged == []


def test_retrieve_with_empty_query_returns_nothing(settings):
    index = MemoryIndex(settings)
    assert index.retrieve("") == []
    assert index.retrieve("   ") == []


async def test_max_entries_bounds_the_index(settings, event_bus: EventBus):
    index = MemoryIndex(settings, max_entries=2)
    index.attach(event_bus)
    for i in range(4):
        await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note=f"note number {i}"))
    await event_bus.drain()
    assert index.statistics()["entries"] == 2
    all_entries = index.all()
    assert [e.text for e in all_entries] == ["note number 2", "note number 3"]
    await event_bus.shutdown()


async def test_disabled_memory_index_never_indexes_anything(settings, event_bus: EventBus):
    settings.memory_index.enabled = False
    index = MemoryIndex(settings)
    index.attach(event_bus)
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="Should not be indexed."))
    await event_bus.drain()
    assert index.statistics()["entries"] == 0
    health = await index.health()
    assert health["status"] == "degraded"
    await event_bus.shutdown()


async def test_health_diagnostics_statistics_shapes(settings, event_bus: EventBus):
    index = MemoryIndex(settings)
    index.attach(event_bus)
    await event_bus.publish(JournalCreated(source="test", symbol="NVDA", note="A note."))
    await event_bus.drain()

    health = await index.health()
    assert health["status"] == "healthy"
    diagnostics = index.diagnostics()
    assert diagnostics["entries_by_tag"].get("journal_note") == 1
    stats = index.statistics()
    assert stats["entries"] == 1
    await event_bus.shutdown()
