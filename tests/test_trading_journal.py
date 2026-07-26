"""Unit tests for the Trading Journal (``app/journal/engine.py``) -- the
Milestone 10 subsystem that enriches Decision Timeline records with
reflections, user notes, and screenshot placeholders, purely via events.
"""
from __future__ import annotations

from app.core.clock import SimulatedClock
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, JournalCreated, ReflectionGenerated
from app.journal.engine import TradingJournal


def _decision(**overrides) -> DecisionRecorded:
    defaults = dict(source="SimulationEngine", symbol="NVDA", simulated_action="watch_bullish", bar_index=5, lookahead_bars=10)
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


def _reflection(decision: DecisionRecorded, **overrides) -> ReflectionGenerated:
    defaults = dict(
        source="ReflectionEngine",
        symbol=decision.symbol,
        decision_event_id=decision.event_id,
        reasoning="test reasoning",
        lessons_learned="test lesson",
        potential_improvements="test improvement",
    )
    defaults.update(overrides)
    return ReflectionGenerated(**defaults)


async def test_journal_entry_created_from_decision_recorded(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    decision = _decision()
    await event_bus.publish(decision)
    await event_bus.drain()

    assert journal.total_entries == 1
    entry = journal.get(decision.event_id)
    assert entry is not None
    assert entry.symbol == "NVDA"
    assert entry.decision.event_id == decision.event_id
    assert entry.reflection is None
    assert entry.notes == []
    assert entry.screenshots == []
    assert entry.broker_execution is None  # honest placeholder, never fabricated


async def test_reflection_attaches_to_the_matching_entry_by_decision_event_id(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    decision = _decision()
    await event_bus.publish(decision)
    await event_bus.drain()

    reflection = _reflection(decision, outcome="correct")
    await event_bus.publish(reflection)
    await event_bus.drain()

    entry = journal.get(decision.event_id)
    assert entry.reflection is not None
    assert entry.reflection.decision_event_id == decision.event_id
    assert entry.reflection.outcome == "correct"


async def test_reflection_for_unmatched_decision_is_an_honest_no_op(settings, event_bus: EventBus):
    """A reflection for a decision this Journal instance never saw (e.g.
    evicted, or from a different run) is silently dropped -- not an
    error -- since the durable event log still has both events."""
    from uuid import uuid4

    journal = TradingJournal(settings)
    journal.attach(event_bus)

    orphan = ReflectionGenerated(source="ReflectionEngine", symbol="NVDA", decision_event_id=uuid4())
    await event_bus.publish(orphan)
    await event_bus.drain()

    assert journal.total_entries == 0  # no crash, no phantom entry


async def test_note_with_decision_event_id_attaches_to_that_entry(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    decision = _decision()
    await event_bus.publish(decision)
    await event_bus.drain()

    await journal.add_note(symbol="NVDA", text="Watching closely", decision_event_id=decision.event_id, author="tavion")
    await event_bus.drain()

    entry = journal.get(decision.event_id)
    assert len(entry.notes) == 1
    assert entry.notes[0].text == "Watching closely"
    assert entry.notes[0].author == "tavion"
    assert journal.general_notes_for("NVDA") == []


async def test_note_without_decision_event_id_becomes_a_general_note(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    await journal.add_note(symbol="NVDA", text="General observation about NVDA")
    await event_bus.drain()

    assert journal.total_entries == 0
    general = journal.general_notes_for("NVDA")
    assert len(general) == 1
    assert general[0].text == "General observation about NVDA"


async def test_screenshot_placeholder_attaches_like_a_note(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    decision = _decision()
    await event_bus.publish(decision)
    await event_bus.drain()

    await journal.add_screenshot(symbol="NVDA", screenshot_url="https://example.com/chart.png", decision_event_id=decision.event_id)
    await event_bus.drain()

    entry = journal.get(decision.event_id)
    assert entry.screenshots == ["https://example.com/chart.png"]


async def test_general_screenshot_without_decision_event_id(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    await journal.add_screenshot(symbol="NVDA", screenshot_url="https://example.com/chart.png")
    await event_bus.drain()

    assert journal.general_screenshots_for("NVDA") == ["https://example.com/chart.png"]


async def test_journal_created_with_no_symbol_is_ignored(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    await event_bus.publish(JournalCreated(source="test", symbol=None, note="orphaned note"))
    await event_bus.drain()

    assert journal.total_entries == 0
    assert journal.symbols() == []


async def test_add_note_without_attached_event_bus_is_a_graceful_no_op():
    from app.config import get_settings

    journal = TradingJournal(get_settings())
    # Never attached -- add_note must not raise.
    await journal.add_note(symbol="NVDA", text="orphaned")
    assert journal.total_entries == 0


async def test_entries_bounded_per_symbol(settings, event_bus: EventBus):
    settings.journal.max_entries_per_symbol = 3
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    decisions = [_decision(bar_index=i) for i in range(5)]
    for decision in decisions:
        await event_bus.publish(decision)
    await event_bus.drain()

    entries = journal.for_symbol("NVDA")
    assert len(entries) == 3
    # The oldest two were evicted -- their entries are gone entirely.
    assert journal.get(decisions[0].event_id) is None
    assert journal.get(decisions[1].event_id) is None
    assert journal.get(decisions[-1].event_id) is not None


async def test_notes_per_entry_bounded(settings, event_bus: EventBus):
    settings.journal.max_notes_per_entry = 2
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    decision = _decision()
    await event_bus.publish(decision)
    await event_bus.drain()

    for i in range(4):
        await journal.add_note(symbol="NVDA", text=f"note {i}", decision_event_id=decision.event_id)
    await event_bus.drain()

    entry = journal.get(decision.event_id)
    assert len(entry.notes) == 2
    assert entry.notes[-1].text == "note 3"  # most recent notes retained


async def test_for_symbol_and_all_and_symbols_queries(settings, event_bus: EventBus):
    journal = TradingJournal(settings)
    journal.attach(event_bus)

    await event_bus.publish(_decision(symbol="NVDA"))
    await event_bus.publish(_decision(symbol="AAPL"))
    await event_bus.drain()

    assert journal.symbols() == ["AAPL", "NVDA"]
    assert len(journal.for_symbol("NVDA")) == 1
    assert len(journal.all()) == 2


async def test_clock_injection_controls_add_note_timestamp(settings, event_bus: EventBus):
    clock = SimulatedClock()
    journal = TradingJournal(settings, clock=clock)
    journal.attach(event_bus)

    await journal.add_note(symbol="NVDA", text="general note")
    await event_bus.drain()

    note = journal.general_notes_for("NVDA")[0]
    assert note.added_at == clock.now()
