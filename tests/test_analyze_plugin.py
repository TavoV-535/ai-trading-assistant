"""
Tests for the /analyze command plugin (plugins/commands/analyze/plugin.py).

Exercises ``execute()`` directly against real ``EvidenceAggregator``,
``StrategyEngine``, and ``ReasoningEngine`` instances wired through a real
event bus — the same "real components, crafted evidence" pattern
``tests/test_pipeline_integration.py`` uses for the full pipeline, scoped
down here to this one command's query/formatting behavior.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.aggregation.aggregator import EvidenceAggregator
from app.context.engine import MarketContextEngine
from app.discord.dispatch import CommandContext
from app.event_bus import EvidenceProduced, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.plugins.base import PluginContext
from app.portfolio.engine import PortfolioIntelligenceEngine
from app.reasoning.engine import ReasoningEngine
from app.strategy.engine import StrategyEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_analyze_plugin_class():
    """Loaded the same way app.plugins.loader discovers it — a plain module
    load by path, not a package import, since plugins/ isn't on sys.path as
    a package (see app/plugins/loader.py::_load_module)."""
    plugin_py = PROJECT_ROOT / "plugins" / "commands" / "analyze" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_analyze_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AnalyzePlugin


AnalyzePlugin = _load_analyze_plugin_class()


def _evidence(source, title, direction, score=10, confidence=70, symbol="NVDA"):
    return Evidence(
        source=source,
        category=EvidenceCategory.TREND,
        title=title,
        score=score,
        confidence=confidence,
        direction=direction,
        symbol=symbol,
    )


async def test_analyze_requires_a_symbol(event_bus, settings):
    plugin = AnalyzePlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await plugin.execute(ctx)

    assert "Usage" in response.content
    assert response.ephemeral is True
    assert response.buttons == []


async def test_analyze_gracefully_degrades_without_core_services(event_bus, settings):
    """PluginContext's evidence_aggregator/reasoning_engine default to
    None — the command must say so plainly, never crash or fabricate."""
    plugin = AnalyzePlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "isn't available" in response.content
    assert response.ephemeral is True


async def test_analyze_reports_insufficient_evidence_for_unseen_symbol(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={},
            evidence_aggregator=aggregator,
            reasoning_engine=reasoning_engine,
        )
    )
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "ghost"})
    response = await plugin.execute(ctx)

    assert "insufficient_evidence" in response.content
    assert "No evidence has been gathered" in response.content
    # Buttons are still offered even with no evidence yet -- Watch/Dismiss
    # are meaningful regardless of whether there's anything to show today.
    assert len(response.buttons) == 7


async def test_analyze_reports_matched_strategy_and_evidence_counts(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    strategy_engine = StrategyEngine(settings)
    strategy_engine.load(PROJECT_ROOT)  # real reference "Momentum Breakout" strategy
    strategy_engine.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={},
            evidence_aggregator=aggregator,
            reasoning_engine=reasoning_engine,
            strategy_engine=strategy_engine,
        )
    )
    await plugin.initialize()

    # Momentum Breakout requires both of these titles and minimum_score: 32;
    # 20 + 15 = 35 clears it, and neither carries repeat-policy metadata, so
    # the Donchian "after_pullback" filter fails open (see
    # tests/test_strategy_engine.py).
    await event_bus.publish(
        EvidenceProduced(source="EMA", evidence=_evidence("EMA", "Bullish EMA Cross", "bullish", score=20))
    )
    await event_bus.publish(
        EvidenceProduced(
            source="Donchian",
            evidence=_evidence("Donchian", "Donchian Channel Breakout (New High)", "bullish", score=15),
        )
    )
    await asyncio.sleep(0.1)

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "NVDA analysis" in response.content
    assert "Momentum Breakout" in response.content
    assert "2 active" in response.content
    assert "2 bullish" in response.content
    assert "conflicting" not in response.content

    labels = [b.label for b in response.buttons]
    assert labels == ["Chart", "News", "History", "Backtest", "Journal", "Watch", "Dismiss"]
    assert response.buttons[-1].custom_id == "dismiss:NVDA"
    assert response.buttons[-1].style == "danger"


async def test_analyze_flags_conflicting_evidence(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={},
            evidence_aggregator=aggregator,
            reasoning_engine=reasoning_engine,
        )
    )
    await plugin.initialize()

    await event_bus.publish(
        EvidenceProduced(source="EMA", evidence=_evidence("EMA", "Bullish EMA Cross", "bullish", score=20))
    )
    await event_bus.publish(
        EvidenceProduced(source="RSI", evidence=_evidence("RSI", "RSI Overbought", "bearish", score=10))
    )
    await asyncio.sleep(0.1)

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "conflicting signals present" in response.content


async def test_analyze_shows_market_context_and_weighted_evidence(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)
    settings.context.trend_window = 6
    settings.context.trend_bull_threshold_pct = 2.0
    context_engine = MarketContextEngine(settings)
    context_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={},
            evidence_aggregator=aggregator,
            reasoning_engine=reasoning_engine,
            context_engine=context_engine,
        )
    )
    await plugin.initialize()

    # Real price data drives the Market Context Engine to derive "Bull
    # Trend" itself -- context isn't something a command can inject, only
    # something the engine computes from data flowing through the bus.
    for price in (100, 101, 102, 103, 104):
        await event_bus.publish(MarketDataUpdated(source="test", symbol="NVDA", price=price, close=price))
    await event_bus.publish(
        EvidenceProduced(source="EMA", evidence=_evidence("EMA", "Bullish EMA Cross", "bullish", score=20))
    )
    await asyncio.sleep(0.1)

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "**Market context:**" in response.content
    assert "Bull Trend" in response.content
    assert "Confidence Weighting Framework" in response.content
    assert "EMA" in response.content


async def test_analyze_without_context_engine_omits_market_context_line(event_bus, settings):
    """context_engine defaults to None -- must degrade gracefully, never crash."""
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus, settings=settings, plugin_config={},
            evidence_aggregator=aggregator, reasoning_engine=reasoning_engine,
        )
    )
    await plugin.initialize()

    await event_bus.publish(
        EvidenceProduced(source="EMA", evidence=_evidence("EMA", "Bullish EMA Cross", "bullish", score=20))
    )
    await asyncio.sleep(0.1)

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "**Market context:**" not in response.content


async def test_analyze_shows_watchlist_priority_when_symbol_is_on_the_watchlist(event_bus, settings):
    settings.portfolio.watchlist = ["NVDA"]
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)
    portfolio_engine = PortfolioIntelligenceEngine(settings)
    portfolio_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus, settings=settings, plugin_config={},
            evidence_aggregator=aggregator, reasoning_engine=reasoning_engine,
            portfolio_engine=portfolio_engine,
        )
    )
    await plugin.initialize()

    await event_bus.publish(
        EvidenceProduced(source="EMA", evidence=_evidence("EMA", "Bullish EMA Cross", "bullish", score=20))
    )
    await asyncio.sleep(0.1)

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "**Watchlist priority:**" in response.content


async def test_analyze_omits_watchlist_priority_when_symbol_is_not_on_the_watchlist(event_bus, settings):
    settings.portfolio.watchlist = ["AAPL"]  # NVDA deliberately excluded
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)
    portfolio_engine = PortfolioIntelligenceEngine(settings)
    portfolio_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus, settings=settings, plugin_config={},
            evidence_aggregator=aggregator, reasoning_engine=reasoning_engine,
            portfolio_engine=portfolio_engine,
        )
    )
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "**Watchlist priority:**" not in response.content


async def test_analyze_without_portfolio_engine_omits_watchlist_priority_line(event_bus, settings):
    """portfolio_engine defaults to None -- must degrade gracefully, never crash."""
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)

    plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus, settings=settings, plugin_config={},
            evidence_aggregator=aggregator, reasoning_engine=reasoning_engine,
        )
    )
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await plugin.execute(ctx)

    assert "**Watchlist priority:**" not in response.content
