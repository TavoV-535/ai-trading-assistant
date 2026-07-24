"""
Tests for the /watchlist command plugin (plugins/commands/watchlist/plugin.py).

Same "load the plugin module by path, exercise execute() against real
components on a real event bus" pattern tests/test_analyze_plugin.py uses.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.discord.dispatch import CommandContext
from app.event_bus.events import EvidenceAggregated, MarketContextUpdated, StrategyMatched, WeightedEvidenceEvent
from app.evidence.schema import Evidence, EvidenceCategory
from app.plugins.base import PluginContext
from app.portfolio.engine import PortfolioIntelligenceEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_watchlist_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "commands" / "watchlist" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_watchlist_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WatchlistPlugin


WatchlistPlugin = _load_watchlist_plugin_class()


def _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish", symbol="NVDA"):
    return Evidence(source=source, category=EvidenceCategory.TREND, title=title, score=10, confidence=80, direction=direction, symbol=symbol)


async def test_watchlist_gracefully_degrades_without_portfolio_engine(event_bus, settings):
    plugin = WatchlistPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))

    assert "isn't available" in response.content
    assert response.ephemeral is True


async def test_watchlist_reports_no_symbols_configured(event_bus, settings):
    settings.portfolio.watchlist = []
    portfolio_engine = PortfolioIntelligenceEngine(settings)
    portfolio_engine.attach(event_bus)

    plugin = WatchlistPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, portfolio_engine=portfolio_engine)
    )
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))

    assert "No symbols are currently configured" in response.content


async def test_watchlist_ranks_symbols_and_shows_breakdown(event_bus, settings):
    settings.portfolio.watchlist = ["NVDA", "AAPL"]
    portfolio_engine = PortfolioIntelligenceEngine(settings)
    portfolio_engine.attach(event_bus)

    strong = _evidence(source="EMA", title="Bullish EMA Cross", symbol="NVDA")
    await event_bus.publish(
        EvidenceAggregated(
            source="test", symbol="NVDA", evidence=strong, active_evidence=[strong],
            weighted_evidence=[WeightedEvidenceEvent(evidence=strong, weight=0.9, breakdown={})],
        )
    )
    await event_bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="NVDA", score=90))
    await event_bus.publish(MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend"))
    await asyncio.sleep(0.05)

    plugin = WatchlistPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, portfolio_engine=portfolio_engine)
    )
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))

    assert "**Watchlist**" in response.content
    assert "1. NVDA" in response.content
    assert "Momentum Breakout" in response.content
    assert "Bull Trend" in response.content
    assert "Score breakdown" in response.content
    # AAPL has zero activity but must still appear, at a lower rank.
    assert "AAPL" in response.content
    assert response.content.index("1. NVDA") < response.content.index("AAPL")
    assert [b.label for b in response.buttons] == ["Refresh", "Dismiss"]
