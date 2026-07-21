from __future__ import annotations

import asyncio
from pathlib import Path

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.plugins import PluginRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def test_ema_plugin_discovered_and_initialized(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    assert "EMA" in registry.plugins
    assert registry.failed == {}

    health = await registry.health_check_all()
    assert health["EMA"].status in ("healthy", "degraded")

    await registry.shutdown_all()


async def test_ema_plugin_publishes_indicator_and_evidence_on_cross(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    indicators = []
    evidence = []

    async def on_indicator(event: IndicatorCalculated) -> None:
        indicators.append(event)

    async def on_evidence(event: EvidenceProduced) -> None:
        evidence.append(event)

    event_bus.subscribe(IndicatorCalculated, on_indicator)
    event_bus.subscribe(EvidenceProduced, on_evidence)

    prices = [100 + i * 1.5 for i in range(30)]  # steady uptrend -> forces a bullish cross
    for p in prices:
        await event_bus.publish(MarketDataUpdated(symbol="NVDA", price=p, timeframe="1m"))
    await asyncio.sleep(0.1)

    assert len(indicators) == len(prices)
    assert any(e.value for e in indicators)
    assert len(evidence) >= 1

    ev = evidence[0].evidence
    assert ev.source == "EMA"
    assert ev.category == "Trend"
    assert ev.direction == "bullish"
    assert ev.symbol == "NVDA"
    assert 0 <= ev.confidence <= 100

    await registry.shutdown_all()
