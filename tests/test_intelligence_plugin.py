"""
Tests for the External Intelligence Platform's shared plugin contract
(app/intelligence/plugin.py) — the polling loop, failure isolation, and
the (intelligence event, evidence) pairing helper every concrete
intelligence plugin (News/Earnings/Macro) builds on.
"""
from __future__ import annotations

import asyncio

from app.event_bus.events import EvidenceProduced, NewsReceived
from app.evidence.schema import Evidence, EvidenceCategory
from app.intelligence.plugin import IntelligencePlugin
from app.plugins.base import PluginContext, PluginPermission


class _CountingPlugin(IntelligencePlugin):
    name = "CountingIntelligence"

    def __init__(self, context) -> None:
        super().__init__(context)
        self.poll_calls = 0

    async def poll_once(self) -> None:
        self.poll_calls += 1


class _FailingPlugin(IntelligencePlugin):
    name = "FailingIntelligence"

    async def poll_once(self) -> None:
        raise RuntimeError("simulated intelligence source outage")


class _PublishingPlugin(IntelligencePlugin):
    name = "PublishingIntelligence"

    async def poll_once(self) -> None:
        event = NewsReceived(source=self.name, headline="Test headline", symbol="NVDA", sentiment="bullish")
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.NEWS,
            title="Test headline",
            score=10,
            confidence=60,
            direction="bullish",
            symbol="NVDA",
        )
        await self._publish(event, evidence)


def _context(event_bus, settings, *, plugin_config=None) -> PluginContext:
    return PluginContext(event_bus=event_bus, settings=settings, plugin_config=plugin_config or {})


async def test_base_poll_once_does_nothing_by_default(event_bus, settings):
    plugin = IntelligencePlugin(_context(event_bus, settings))
    await plugin.poll_once()  # must not raise -- degrades to "publishes nothing"


async def test_initialize_starts_a_background_loop_that_ticks_repeatedly(event_bus, settings):
    plugin = _CountingPlugin(_context(event_bus, settings, plugin_config={"interval_seconds": 0.02}))

    await plugin.initialize()
    await asyncio.sleep(0.15)
    await plugin.shutdown()

    assert plugin.poll_calls >= 3
    health = await plugin.health()
    assert health.status == "healthy"


async def test_a_failing_poll_is_isolated_and_retried(event_bus, settings):
    plugin = _FailingPlugin(_context(event_bus, settings, plugin_config={"interval_seconds": 0.02}))

    await plugin.initialize()
    await asyncio.sleep(0.1)
    await plugin.shutdown()  # must not raise / crash the loop

    health = await plugin.health()
    assert health.status == "degraded"
    assert "simulated intelligence source outage" in health.detail


async def test_publish_pairs_intelligence_event_and_evidence(event_bus, settings):
    plugin = _PublishingPlugin(_context(event_bus, settings, plugin_config={"interval_seconds": 10}))

    news_events: list[NewsReceived] = []
    evidence_events: list[EvidenceProduced] = []
    event_bus.subscribe(NewsReceived, lambda e: news_events.append(e))
    event_bus.subscribe(EvidenceProduced, lambda e: evidence_events.append(e))

    await plugin.poll_once()
    await asyncio.sleep(0.05)

    assert len(news_events) == 1
    assert len(evidence_events) == 1
    assert news_events[0].headline == evidence_events[0].evidence.title
    assert evidence_events[0].evidence.source == plugin.name


async def test_config_uses_settings_default_interval_when_unconfigured(event_bus, settings):
    settings.intelligence.interval_seconds = 42
    plugin = IntelligencePlugin(_context(event_bus, settings))
    assert plugin.interval_seconds == 42
    assert plugin.config() == {"interval_seconds": 42.0}


async def test_permissions_declare_network_and_publish():
    from app.plugins.base import PluginContext as _PC

    class _DummyEventBus:
        pass

    ctx = _PC(
        event_bus=_DummyEventBus(),
        settings=type("S", (), {"intelligence": type("I", (), {"interval_seconds": 60})()})(),
        plugin_config={},
    )
    plugin = IntelligencePlugin(ctx)
    assert PluginPermission.NETWORK_OUTBOUND in plugin.permissions()
    assert PluginPermission.EVENTS_PUBLISH in plugin.permissions()
