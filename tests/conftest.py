from __future__ import annotations

import pytest

from app.config import get_settings
from app.event_bus.bus import EventBus


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch):
    """Every test gets a fresh Settings instance pointed at an in-memory
    SQLite database, so the test suite never depends on a running Postgres.

    Also shadows every secret field with an empty env var. ``Settings``
    reads a real ``.env`` in the project root if one exists (by design —
    that's how a deployment picks up its real Discord/Anthropic tokens), but
    that means the moment a developer follows docs/DISCORD_BOT_SETUP.md and
    creates a real .env locally, the test suite would otherwise pick up
    their real secrets and produce different (and confusing) results than
    CI. Environment variables outrank the .env file
    (``settings_customise_sources`` in app/config/settings.py), so setting
    these to "" here always wins regardless of what's on disk. A test that
    specifically wants to exercise "a token is configured" sets its own env
    var via ``monkeypatch.setenv`` after this fixture runs.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    for var in ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "ANTHROPIC_API_KEY", "POLYGON_API_KEY", "FINNHUB_API_KEY"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def event_bus(settings):
    return EventBus.from_settings(settings)
