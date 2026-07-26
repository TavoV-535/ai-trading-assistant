from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.db import Database, EventLog, EventLogRepository, attach_event_logger
from app.event_bus import DecisionRecorded, EventBus, JournalCreated, MarketDataUpdated, ReflectionGenerated


async def test_database_health_and_create_all(settings):
    db = Database(settings)
    await db.create_all()
    assert await db.health() is True
    await db.dispose()


async def test_event_logger_persists_published_events(settings, event_bus: EventBus):
    db = Database(settings)
    await db.create_all()
    attach_event_logger(event_bus, db)

    await event_bus.publish(MarketDataUpdated(symbol="AAPL", price=210.5, volume=500, source="test"))
    await event_bus.publish(MarketDataUpdated(symbol="MSFT", price=410.2, volume=700, source="test"))
    await asyncio.sleep(0.1)

    async with db.session() as session:
        repo = EventLogRepository(session)
        rows = await repo.recent(limit=10)
        assert len(rows) == 2
        symbols = {r.payload.get("symbol") for r in rows}
        assert symbols == {"AAPL", "MSFT"}

        count = await repo.count(event_type="MarketDataUpdated")
        assert count == 2

    await event_bus.shutdown()
    await db.dispose()


async def test_repository_crud(settings):
    db = Database(settings)
    await db.create_all()

    async with db.session() as session:
        repo = EventLogRepository(session)
        row = await repo.add(
            EventLog(
                event_id=uuid4(),
                event_type="TestEvent",
                source="unit-test",
                payload={"hello": "world"},
                created_at=datetime.now(timezone.utc),
            )
        )
        assert row.id is not None

    async with db.session() as session:
        repo = EventLogRepository(session)
        fetched = await repo.get(row.id)
        assert fetched is not None
        assert fetched.payload == {"hello": "world"}
        await repo.delete(fetched)

    async with db.session() as session:
        repo = EventLogRepository(session)
        assert await repo.get(row.id) is None

    await db.dispose()


async def test_decision_records_reconstructed_from_durable_event_log(settings, event_bus: EventBus):
    """The Decision Timeline's canonical historical record needs no
    dedicated table -- every DecisionRecorded event is already persisted
    verbatim by attach_event_logger (Repository pattern), and
    EventLogRepository.decision_records() reconstructs DecisionRecord
    objects straight from those durable rows."""
    db = Database(settings)
    await db.create_all()
    attach_event_logger(event_bus, db)

    await event_bus.publish(
        DecisionRecorded(
            source="SimulationEngine",
            symbol="NVDA",
            market_context={"trend": "Bull Trend"},
            technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
            fundamental_evidence=["News: NVDA beats estimates (bullish, 70/100)"],
            confidence_weights={"EMA:Bullish EMA Cross": 0.62},
            strategy_matches=["Momentum Breakout"],
            reasoning_summary="Bullish evidence dominates.",
            reasoning_source="evidence_only",
            confidence=71.5,
            simulated_action="watch_bullish",
            price_at_decision=123.45,
            bar_index=5,
            lookahead_bars=10,
            outcome="correct",
            outcome_price_change_pct=1.2,
            outcome_pending=False,
        )
    )
    await event_bus.publish(
        DecisionRecorded(source="SimulationEngine", symbol="AAPL", bar_index=5, lookahead_bars=10)
    )
    await asyncio.sleep(0.1)

    async with db.session() as session:
        repo = EventLogRepository(session)

        all_records = await repo.decision_records()
        assert len(all_records) == 2

        nvda_records = await repo.decision_records(symbol="NVDA")
        assert len(nvda_records) == 1
        record = nvda_records[0]
        assert record.symbol == "NVDA"
        assert record.market_context == {"trend": "Bull Trend"}
        assert record.technical_evidence == ["EMA: Bullish EMA Cross (bullish, 80/100)"]
        assert record.fundamental_evidence == ["News: NVDA beats estimates (bullish, 70/100)"]
        assert record.strategy_matches == ["Momentum Breakout"]
        assert record.simulated_action == "watch_bullish"
        assert record.outcome == "correct"
        assert record.outcome_pending is False

        aapl_records = await repo.decision_records(symbol="AAPL")
        assert len(aapl_records) == 1
        assert aapl_records[0].outcome is None
        assert aapl_records[0].outcome_pending is True

    await event_bus.shutdown()
    await db.dispose()


async def test_reflections_reconstructed_from_durable_event_log(settings, event_bus: EventBus):
    """Mirrors test_decision_records_reconstructed_from_durable_event_log
    -- Milestone 10's ReflectionGenerated needs no dedicated table either;
    EventLogRepository.reflections() reconstructs ReflectionRecord objects
    straight from the durable event_log rows."""
    db = Database(settings)
    await db.create_all()
    attach_event_logger(event_bus, db)

    decision_id = uuid4()
    await event_bus.publish(
        ReflectionGenerated(
            source="ReflectionEngine",
            symbol="NVDA",
            decision_event_id=decision_id,
            reasoning="Bullish evidence dominated.",
            supporting_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
            contradictory_evidence=["VWAP: Price Crossed Below VWAP (bearish, 60/100)"],
            market_context={"trend": "Bull Trend"},
            confidence=71.5,
            confidence_evolution="rising",
            simulated_action="watch_bullish",
            outcome="correct",
            outcome_price_change_pct=1.5,
            lessons_learned="The bullish hypothesis held.",
            potential_improvements="Continue monitoring.",
        )
    )
    await event_bus.publish(ReflectionGenerated(source="ReflectionEngine", symbol="AAPL", decision_event_id=uuid4()))
    await asyncio.sleep(0.1)

    async with db.session() as session:
        repo = EventLogRepository(session)

        all_reflections = await repo.reflections()
        assert len(all_reflections) == 2

        nvda_reflections = await repo.reflections(symbol="NVDA")
        assert len(nvda_reflections) == 1
        record = nvda_reflections[0]
        assert record.symbol == "NVDA"
        assert record.decision_event_id == decision_id
        assert record.reasoning == "Bullish evidence dominated."
        assert record.supporting_evidence == ["EMA: Bullish EMA Cross (bullish, 80/100)"]
        assert record.contradictory_evidence == ["VWAP: Price Crossed Below VWAP (bearish, 60/100)"]
        assert record.confidence_evolution == "rising"
        assert record.outcome == "correct"

    await event_bus.shutdown()
    await db.dispose()


async def test_journal_notes_reconstructed_from_durable_event_log(settings, event_bus: EventBus):
    """Mirrors the decision_records/reflections repository tests --
    EventLogRepository.journal_notes() reconstructs JournalNote objects
    straight from durable JournalCreated event_log rows, with optional
    symbol/decision_event_id narrowing."""
    db = Database(settings)
    await db.create_all()
    attach_event_logger(event_bus, db)

    decision_id = uuid4()
    await event_bus.publish(
        JournalCreated(source="TradingJournal", symbol="NVDA", decision_event_id=decision_id, note="Watching closely", author="tavion")
    )
    await event_bus.publish(JournalCreated(source="TradingJournal", symbol="NVDA", note="General NVDA observation"))
    await event_bus.publish(JournalCreated(source="TradingJournal", symbol="AAPL", note="AAPL note"))
    # A screenshot-only event has no `note` -- journal_notes() only ever
    # returns actual text notes, never a placeholder screenshot entry.
    await event_bus.publish(JournalCreated(source="TradingJournal", symbol="NVDA", screenshot_url="https://example.com/chart.png"))
    await asyncio.sleep(0.1)

    async with db.session() as session:
        repo = EventLogRepository(session)

        nvda_notes = await repo.journal_notes(symbol="NVDA")
        assert len(nvda_notes) == 2
        assert {n.text for n in nvda_notes} == {"Watching closely", "General NVDA observation"}

        scoped_notes = await repo.journal_notes(symbol="NVDA", decision_event_id=decision_id)
        assert len(scoped_notes) == 1
        assert scoped_notes[0].text == "Watching closely"
        assert scoped_notes[0].author == "tavion"

        all_notes = await repo.journal_notes()
        assert len(all_notes) == 3  # the screenshot-only event is excluded

    await event_bus.shutdown()
    await db.dispose()
