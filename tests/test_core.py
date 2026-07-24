from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.app import create_app
from app.core.bootstrap import bootstrap, teardown

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def test_bootstrap_loads_plugins_and_teardown_is_clean(settings):
    state = await bootstrap(settings, project_root=PROJECT_ROOT)

    assert "EMA" in state.plugin_registry.plugins
    assert "Ping" in state.plugin_registry.plugins
    assert state.plugin_registry.failed == {}
    assert await state.database.health() is True
    assert state.discord_bot is None  # no DISCORD_BOT_TOKEN in test settings
    assert state.discord_task is None

    await teardown(state)


def test_health_endpoint_reports_healthy(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["database"] == "healthy"
        assert body["discord"] == "not_configured"  # no DISCORD_BOT_TOKEN in test settings
        assert body["plugins"]["EMA"] == "degraded"  # no market data fed yet in this test
        assert body["plugins"]["Ping"] == "healthy"
        assert body["plugins_failed_to_load"] == []


def test_plugins_endpoint_lists_ema_and_ping(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/plugins")
        assert response.status_code == 200
        body = response.json()
        assert "EMA" in body["loaded"]
        assert body["loaded"]["EMA"]["category"] == "indicators"
        assert "Ping" in body["loaded"]
        assert body["loaded"]["Ping"]["category"] == "commands"
        assert body["failed"] == {}


def test_watchlist_endpoint_reports_configured_symbols(settings):
    settings.portfolio.watchlist = ["NVDA", "AAPL"]
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/watchlist")
        assert response.status_code == 200
        body = response.json()
        assert body["watchlist"] == ["NVDA", "AAPL"]
        assert {p["symbol"] for p in body["ranked"]} == {"NVDA", "AAPL"}
        # Untouched watchlist symbols still report a (zero) priority score,
        # not an error -- continuous monitoring, not "only if active."
        assert all(p["priority_score"] == 0.0 for p in body["ranked"])
