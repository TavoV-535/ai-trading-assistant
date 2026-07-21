from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.db import Database, EventLog, EventLogRepository, attach_event_logger
from app.event_bus import EventBus, MarketDataUpdated


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
