"""
Tests for the reference News intelligence plugin
(plugins/intelligence/news/plugin.py) — a concrete example of the
External Intelligence Platform contract, not a special-cased subsystem.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.event_bus.events import EvidenceProduced, NewsReceived
from app.plugins.base import PluginContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_news_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "intelligence" / "news" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_news_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NewsPlugin


NewsPlugin = _load_news_plugin_class()


async def test_news_plugin_publishes_headline_and_evidence_pairs(event_bus, settings):
    plugin = NewsPlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={"watchlist": ["NVDA", "AAPL"], "interval_seconds": 10},
        )
    )

    news_events: list[NewsReceived] = []
    evidence_events: list[EvidenceProduced] = []
    event_bus.subscribe(NewsReceived, lambda e: news_events.append(e))
    event_bus.subscribe(EvidenceProduced, lambda e: evidence_events.append(e))

    for _ in range(30):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    assert len(news_events) > 0
    assert len(news_events) == len(evidence_events)
    for news, ev in zip(news_events, evidence_events):
        assert news.headline == ev.evidence.title
        assert news.provider == "synthetic-news-feed"
        assert news.sentiment == ev.evidence.direction
        assert ev.evidence.symbol in ("NVDA", "AAPL")
        assert ev.evidence.category == "News"
        assert ev.evidence.source == "News"
        assert 0 <= ev.evidence.confidence <= 100


async def test_news_plugin_never_publishes_for_symbols_outside_watchlist(event_bus, settings):
    plugin = NewsPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={"watchlist": ["TSLA"], "interval_seconds": 10})
    )

    news_events: list[NewsReceived] = []
    event_bus.subscribe(NewsReceived, lambda e: news_events.append(e))

    for _ in range(20):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    assert news_events  # sanity: something fired
    assert all(e.symbol == "TSLA" for e in news_events)


async def test_news_plugin_config_reflects_watchlist():
    class _DummyEventBus:
        pass

    ctx = PluginContext(
        event_bus=_DummyEventBus(),
        settings=type("S", (), {"intelligence": type("I", (), {"interval_seconds": 60})()})(),
        plugin_config={"watchlist": ["NVDA"], "interval_seconds": 5},
    )
    plugin = NewsPlugin(ctx)
    assert plugin.config() == {"interval_seconds": 5.0, "watchlist": ["NVDA"]}


async def test_news_plugin_discovered_by_registry(event_bus, settings):
    """Structural check: the News plugin loads through the normal
    plugin-discovery path with no special casing -- it's a folder under
    plugins/intelligence/, nothing more."""
    from app.plugins.registry import PluginRegistry

    settings.plugins.disabled = [d for d in settings.plugins.disabled if d != "News"]
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    assert "News" in registry.plugins
    assert registry.failed == {}
    await registry.shutdown_all()
