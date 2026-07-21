"""
Wires the Event Bus to the database: every event published anywhere in the
system gets persisted to ``event_log``. This is what makes "everything
logged" true at the storage layer, without any plugin needing to know the
database exists.
"""
from __future__ import annotations

from app.db.base import Database
from app.db.repository import EventLogRepository
from app.event_bus.bus import EventBus
from app.event_bus.events import Event
from app.logging import get_logger

log = get_logger(__name__)


def attach_event_logger(event_bus: EventBus, database: Database) -> None:
    """Subscribe a persistence handler to every event on the bus."""

    async def _persist(event: Event) -> None:
        async with database.session() as session:
            await EventLogRepository(session).record_event(event)

    event_bus.subscribe_all(_persist, name="db.event_logger")
    log.info("event_logger_attached")
