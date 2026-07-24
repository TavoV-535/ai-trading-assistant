"""
Milestone 8 completion requirement: prove the full Portfolio & Watchlist
Intelligence Layer + Event Prioritization Engine flow end to end over the
real Event Bus --

    Market Data -> indicator plugins -> Technical Evidence \\
    Strategy Engine ----------------------------------------> StrategyMatched \\
    Market Context Engine -----------------------------------> MarketContextUpdated \\
                                                                         |
                                                        Evidence Aggregator (+ Confidence Weighting)
                                                                         |
                                       -----------------------------------------------------------
                                       |                                                          |
                          Portfolio Intelligence Layer                               Event Prioritization Engine
                          (SymbolProfileUpdated, ranked_watchlist)                    (AlertGenerated, decision_history)
                                       |                                                          |
                                  /watchlist                                              proactive Discord alert
                                       |
                                  /analyze SYMBOL (portfolio snippet)

Nothing here calls a downstream system directly -- every arrow above is a
real event on a real ``EventBus``. Reuses the same crafted bar sequence
``tests/test_pipeline_integration.py`` already validated produces a real
"Momentum Breakout" match and a real "Bull Trend" context for NVDA. AAPL is
deliberately left untouched to demonstrate continuous monitoring of
*multiple* configured symbols, not just whichever one just had activity.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.aggregation.aggregator import EvidenceAggregator
from app.context.engine import MarketContextEngine
from app.discord.dispatch import CommandContext
from app.event_bus.events import AlertGenerated, MarketDataUpdated, SymbolProfileUpdated
from app.plugins import PluginRegistry
from app.plugins.base import PluginContext
from app.portfolio.engine import PortfolioIntelligenceEngine
from app.prioritization.engine import EventPrioritizationEngine
from app.reasoning.engine import ReasoningEngine
from app.strategy.engine import StrategyEngine
from tests.test_pipeline_integration import _bars_for_momentum_breakout

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_class(rel_path: str, module_name: str, class_name: str):
    plugin_py = PROJECT_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


AnalyzePlugin = _load_plugin_class("plugins/commands/analyze/plugin.py", "_test_m8_analyze", "AnalyzePlugin")
WatchlistPlugin = _load_plugin_class("plugins/commands/watchlist/plugin.py", "_test_m8_watchlist", "WatchlistPlugin")


async def _publish_bars(event_bus, symbol, bars, timeframe="1m"):
    for bar in bars:
        await event_bus.publish(
            MarketDataUpdated(
                source="test", symbol=symbol, price=bar["close"], open=bar.get("open"), high=bar.get("high"),
                low=bar.get("low"), close=bar.get("close"), volume=bar.get("volume"), timeframe=timeframe,
            )
        )
    await asyncio.sleep(0.2)


async def test_full_milestone8_pipeline_multi_symbol_monitoring_and_alerts(event_bus, settings):
    active_symbol, quiet_symbol = "NVDA", "AAPL"
    settings.portfolio.watchlist = [active_symbol, quiet_symbol]

    # -- Core systems, wired exactly like app.core.bootstrap ---------------
    context_engine = MarketContextEngine(settings)
    context_engine.attach(event_bus)
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    strategy_engine = StrategyEngine(settings)
    strategy_engine.load(PROJECT_ROOT)
    strategy_engine.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)
    portfolio_engine = PortfolioIntelligenceEngine(settings)
    portfolio_engine.attach(event_bus)
    prioritization_engine = EventPrioritizationEngine(settings)
    prioritization_engine.attach(event_bus)

    profile_updates: list[SymbolProfileUpdated] = []
    event_bus.subscribe(SymbolProfileUpdated, lambda e: profile_updates.append(e))
    alerts: list[AlertGenerated] = []
    event_bus.subscribe(AlertGenerated, lambda e: alerts.append(e))

    # -- Technical evidence: real indicator plugins reacting to real price
    # data, driving a real "Momentum Breakout" StrategyMatched + a real
    # "Bull Trend" MarketContextUpdated for NVDA only -- AAPL never gets a tick.
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT, search_paths=["plugins/indicators"])
    await _publish_bars(event_bus, active_symbol, _bars_for_momentum_breakout())

    # -- Portfolio Intelligence Layer: continuously monitors BOTH configured
    # symbols. The active one gets a real, non-zero, transparently-explained
    # priority score; the quiet one is still tracked (not dropped), just at
    # zero -- "continuous monitoring," not "monitoring on request."
    assert profile_updates, "Portfolio Intelligence Layer never published a SymbolProfileUpdated"
    active_profile = portfolio_engine.snapshot(active_symbol)
    quiet_profile = portfolio_engine.snapshot(quiet_symbol)
    assert active_profile is not None and quiet_profile is not None
    assert active_profile.priority_score > 0.0
    assert active_profile.matched_strategies == ["Momentum Breakout"]
    assert active_profile.context.get("trend") == "Bull Trend"
    assert quiet_profile.priority_score == 0.0

    ranked = portfolio_engine.ranked_watchlist()
    assert [p.symbol for p in ranked] == [active_symbol, quiet_symbol]  # highest priority first

    # -- Event Prioritization Engine: the same StrategyMatched that drove the
    # profile score also cleared the alert threshold -- a real, scored,
    # transparent AlertGenerated, not a hardcoded notification.
    assert alerts, "Event Prioritization Engine never generated an alert for a real strategy match"
    strategy_alerts = [a for a in alerts if a.source_event_type == "StrategyMatched" and a.symbol == active_symbol]
    assert strategy_alerts
    alert = strategy_alerts[0]
    assert alert.score >= settings.prioritization.alert_threshold
    assert set(alert.breakdown) >= {"importance", "novelty", "confidence_change", "urgency", "user_relevance"}

    # The quiet symbol never had a candidate development, so it has no
    # alert decisions recorded at all yet -- correctly nothing to suppress
    # or accept, not silently dropped.
    assert prioritization_engine.decision_history(quiet_symbol) == []
    assert prioritization_engine.decision_history(active_symbol)

    # -- /watchlist surfaces the ranked, prioritized output.
    watchlist_plugin = WatchlistPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, portfolio_engine=portfolio_engine)
    )
    await watchlist_plugin.initialize()
    watchlist_response = await watchlist_plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))
    assert f"1. {active_symbol}" in watchlist_response.content
    assert "Momentum Breakout" in watchlist_response.content
    assert quiet_symbol in watchlist_response.content

    # -- /analyze integrates the Portfolio Intelligence Layer's profile
    # alongside everything Milestone 7 already showed.
    analyze_plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus, settings=settings, plugin_config={},
            evidence_aggregator=aggregator, reasoning_engine=reasoning_engine,
            strategy_engine=strategy_engine, context_engine=context_engine,
            portfolio_engine=portfolio_engine,
        )
    )
    await analyze_plugin.initialize()
    analyze_response = await analyze_plugin.execute(
        CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": active_symbol})
    )
    assert "Momentum Breakout" in analyze_response.content
    assert "Bull Trend" in analyze_response.content
    assert "**Watchlist priority:**" in analyze_response.content

    await registry.shutdown_all()


def test_portfolio_engine_only_imports_generic_modules():
    """Architectural guarantee mirroring Milestone 7's Market Context Engine
    check: the Portfolio Intelligence Layer never imports the Evidence
    Aggregator, Strategy Engine, Reasoning Engine, or the Event
    Prioritization Engine directly -- everything it produces and consumes
    goes through the Event Bus."""
    import ast

    import app.portfolio.engine as portfolio_module

    allowed_prefixes = ("app.event_bus", "app.portfolio", "app.logging")
    tree = ast.parse(Path(portfolio_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            assert node.module.startswith(allowed_prefixes), (
                f"app.portfolio.engine imports {node.module!r} -- the Portfolio Intelligence Layer "
                "must never import another core engine directly"
            )


def test_prioritization_engine_only_imports_generic_modules():
    """Mirrors the same guardrail for the Event Prioritization Engine: it
    never imports the Portfolio Intelligence Layer (or any other core
    engine) directly -- SymbolProfileUpdated reaches it only via the bus."""
    import ast

    import app.prioritization.engine as prioritization_module

    allowed_prefixes = ("app.event_bus", "app.prioritization", "app.logging")
    tree = ast.parse(Path(prioritization_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            assert node.module.startswith(allowed_prefixes), (
                f"app.prioritization.engine imports {node.module!r} -- the Event Prioritization Engine "
                "must never import another core engine directly"
            )
