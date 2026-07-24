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
    """A fresh Settings instance, with the reference scanner and
    intelligence plugins disabled by default.

    ``CoreWatchlistScanner.initialize()`` (Milestone 6) and every
    ``IntelligencePlugin`` (News/Earnings/Macro, Milestone 7) start a real
    background asyncio task the moment they're loaded (by design — that's
    what "run continuously"/"poll on an interval" means for a real
    deployment; see ``app/scanner/plugin.py`` and
    ``app/intelligence/plugin.py``). Left enabled, every test that loads
    the full plugin registry would spin up unwanted long-running tasks.
    Tests that actually want to exercise scanning or intelligence polling
    override ``settings.plugins.disabled`` back to normal, or construct/
    drive the plugin directly with a short ``interval_seconds`` (see
    ``tests/test_scanner_plugin.py``, ``tests/test_intelligence_plugin.py``).
    """
    s = get_settings()
    s.plugins.disabled = [*s.plugins.disabled, "CoreWatchlistScanner", "News", "Earnings", "Macro"]
    return s


@pytest.fixture
def event_bus(settings):
    return EventBus.from_settings(settings)
