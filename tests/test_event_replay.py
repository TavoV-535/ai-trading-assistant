"""Unit tests for the Event Replay API (``app/replay/service.py``) —
Milestone 12. Exercises it against a real durable event log (SQLite via
the existing ``Database``/``EventLogRepository`` — no in-memory
substitute), the same way ``tests/test_db.py`` tests the repository
layer itself."""
from __future__ import annotations

from uuid import uuid4

from app.db.base import Database
from app.db.event_logger import attach_event_logger
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, JournalCreated, ReflectionGenerated, TradeClosed, TradeOpened
from app.replay.service import EventReplayService


async def _seeded_bus_and_db(settings):
    db = Database(settings)
    await db.create_all()
    bus = EventBus.from_settings(settings)
    attach_event_logger(bus, db)
    return db, bus


async def test_replay_decision_reconstructs_the_full_causal_chain(settings):
    db, bus = await _seeded_bus_and_db(settings)
    decision_id = uuid4()
    trade_id = uuid4()
    symbol = "NVDA"

    await bus.publish(DecisionRecorded(
        event_id=decision_id, source="test", symbol=symbol, reasoning_summary="Bullish evidence dominates.",
        confidence=70.0, simulated_action="watch_bullish", price_at_decision=100.0, bar_index=0, lookahead_bars=5,
        outcome=None, outcome_pending=True,
    ))
    await bus.drain()
    await bus.publish(TradeOpened(source="test", decision_event_id=decision_id, trade_id=trade_id, symbol=symbol, side="long", quantity=10, entry_price=100.0))
    await bus.publish(ReflectionGenerated(source="test", symbol=symbol, decision_event_id=decision_id, reasoning="r", lessons_learned="Entry timing was good."))
    await bus.publish(JournalCreated(source="test", symbol=symbol, decision_event_id=decision_id, note="Felt confident."))
    await bus.drain()
    await bus.publish(TradeClosed(source="test", symbol=symbol, exit_price=105.0, trade_id=trade_id, pnl=50.0, decision_event_id=decision_id))
    await bus.drain()

    service = EventReplayService(db)
    replay = await service.replay_decision(decision_id)

    assert replay.decision is not None
    assert replay.decision.symbol == symbol
    assert replay.reflection is not None
    assert replay.reflection.lessons_learned == "Entry timing was good."
    assert [n.text for n in replay.journal_notes] == ["Felt confident."]
    assert replay.trade_opened.entry_price == 100.0
    assert replay.trade_closed.pnl == 50.0

    event_types = [e.event_type for e in replay.timeline]
    assert event_types == ["DecisionRecorded", "TradeOpened", "ReflectionGenerated", "JournalCreated", "TradeClosed"]
    # Chronologically ordered.
    assert replay.timeline == sorted(replay.timeline, key=lambda e: e.timestamp)
    await bus.shutdown()


async def test_replay_decision_for_unknown_id_is_an_honest_none(settings):
    db, bus = await _seeded_bus_and_db(settings)
    service = EventReplayService(db)
    replay = await service.replay_decision(uuid4())
    assert replay.decision is None
    assert replay.reflection is None
    assert replay.journal_notes == []
    assert replay.trade_opened is None
    assert replay.trade_closed is None
    assert replay.timeline == []
    await bus.shutdown()


async def test_replay_decision_never_mixes_up_two_different_decisions(settings):
    """related_to_decision() must only return rows whose payload actually
    references this decision_event_id -- a second, unrelated decision's
    trades/reflections must never bleed into this one's replay."""
    db, bus = await _seeded_bus_and_db(settings)
    decision_a = uuid4()
    decision_b = uuid4()

    await bus.publish(DecisionRecorded(event_id=decision_a, source="test", symbol="NVDA", reasoning_summary="A", confidence=70.0, simulated_action="watch_bullish", price_at_decision=100.0, bar_index=0, lookahead_bars=5, outcome=None, outcome_pending=True))
    await bus.publish(DecisionRecorded(event_id=decision_b, source="test", symbol="AAPL", reasoning_summary="B", confidence=60.0, simulated_action="watch_bearish", price_at_decision=200.0, bar_index=0, lookahead_bars=5, outcome=None, outcome_pending=True))
    await bus.drain()
    await bus.publish(JournalCreated(source="test", symbol="NVDA", decision_event_id=decision_a, note="Note for A"))
    await bus.publish(JournalCreated(source="test", symbol="AAPL", decision_event_id=decision_b, note="Note for B"))
    await bus.drain()

    service = EventReplayService(db)
    replay_a = await service.replay_decision(decision_a)
    assert [n.text for n in replay_a.journal_notes] == ["Note for A"]
    assert replay_a.decision.symbol == "NVDA"
    await bus.shutdown()


async def test_health_diagnostics_statistics_shapes(settings):
    db, bus = await _seeded_bus_and_db(settings)
    service = EventReplayService(db)
    await service.replay_decision(uuid4())

    health = await service.health()
    assert health["status"] == "healthy"
    assert health["database_reachable"] is True

    diagnostics = service.diagnostics()
    assert diagnostics["total_replays"] == 1
    stats = service.statistics()
    assert stats["total_replays"] == 1
    await bus.shutdown()
