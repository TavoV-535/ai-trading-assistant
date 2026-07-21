from __future__ import annotations

import pytest

from app.config import get_settings
from app.event_bus.bus import EventBus


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch):
    """Every test gets a fresh Settings instance pointed at an in-memory
    SQLite database, so the test suite never depends on a running Postgres."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def event_bus(settings):
    return EventBus.from_settings(settings)
