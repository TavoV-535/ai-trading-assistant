"""
Milestone 7 completion requirement: prove the full External Intelligence
Platform + Market Context Engine + Confidence Weighting Framework flow
end to end over the real Event Bus, into a real ``/analyze`` response --

    Market Data ---------------\\
    Scanner/indicator plugins --> Technical Evidence --\\
    External Intelligence (News/Earnings/Macro) -------> Fundamental Evidence --\\
    Market Context Engine ------------------------------> Context (regime input) --> Evidence Aggregator
                                                                                          |
                                                                        Confidence Weighting Framework
                                                                                          |
                                                                                Strategy / Reasoning Engine
                                                                                          |
                                                                                    /analyze SYMBOL

Nothing here calls a downstream system directly -- every arrow above is a
real event on a real ``EventBus``. This test reuses the same crafted bar
sequence ``tests/test_pipeline_integration.py`` already validated produces
a real "Momentum Breakout" match and, per its own comment, spans exactly
the Market Context Engine's default 20-bar trend window with a clean 25%
rise -- so it also deterministically produces a real "Bull Trend" context.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.aggregation.aggregator import EvidenceAggregator
from app.context.engine import MarketContextEngine
from app.discord.dispatch import CommandContext
from app.event_bus.events import EvidenceProduced, MarketDataUpdated
from app.plugins import PluginRegistry
from app.plugins.base import PluginContext
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


NewsPlugin = _load_plugin_class("plugins/intelligence/news/plugin.py", "_test_m7_news", "NewsPlugin")
EarningsPlugin = _load_plugin_class("plugins/intelligence/earnings/plugin.py", "_test_m7_earnings", "EarningsPlugin")
MacroPlugin = _load_plugin_class("plugins/intelligence/macro/plugin.py", "_test_m7_macro", "MacroPlugin")
AnalyzePlugin = _load_plugin_class("plugins/commands/analyze/plugin.py", "_test_m7_analyze", "AnalyzePlugin")


async def _publish_bars(event_bus, symbol, bars, timeframe="1m"):
    for bar in bars:
        await event_bus.publish(
            MarketDataUpdated(
                source="test", symbol=symbol, price=bar["close"], open=bar.get("open"), high=bar.get("high"),
                low=bar.get("low"), close=bar.get("close"), volume=bar.get("volume"), timeframe=timeframe,
            )
        )
    await asyncio.sleep(0.2)


async def test_full_milestone7_pipeline_into_analyze(event_bus, settings):
    symbol = "NVDA"

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

    # -- Technical evidence: real indicator plugins reacting to real price data
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT, search_paths=["plugins/indicators"])

    raw_evidence: list[EvidenceProduced] = []
    event_bus.subscribe(EvidenceProduced, lambda e: raw_evidence.append(e))

    await _publish_bars(event_bus, symbol, _bars_for_momentum_breakout())

    technical_evidence = [e for e in raw_evidence if e.evidence.category != "News" and e.evidence.category != "Earnings" and e.evidence.category != "Macro"]
    assert technical_evidence, "no real indicator plugin published technical evidence"

    # -- Context evidence: the Market Context Engine, reacting to the exact
    # same real price data -- never called directly, only via the bus.
    assert context_engine.snapshot(symbol).get("trend") == "Bull Trend"

    # -- Fundamental evidence: real, independent News/Earnings/Macro plugins
    # (each its own file under plugins/intelligence/, sharing only the
    # generic IntelligencePlugin contract) polled enough times that their
    # seeded-but-deterministic output produces at least one item each for
    # this symbol.
    news = NewsPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={"watchlist": [symbol], "interval_seconds": 10}))
    earnings = EarningsPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={"watchlist": [symbol], "interval_seconds": 10}))
    macro = MacroPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={"interval_seconds": 10}))
    for _ in range(40):
        await news.poll_once()
        await earnings.poll_once()
        await macro.poll_once()
    await asyncio.sleep(0.1)

    fundamental_evidence = [e for e in raw_evidence if e.evidence.source in ("News", "Earnings")]
    assert fundamental_evidence, "News/Earnings plugins never produced evidence for this symbol across 40 polls"
    macro_evidence = [e for e in raw_evidence if e.evidence.source == "Macro"]
    assert macro_evidence, "Macro plugin never produced a market-wide event across 40 polls"

    # -- Confidence Weighting Framework: the aggregator's snapshot carries a
    # normalized weight per item, alongside (never instead of) the raw
    # evidence -- and weights actually differ, proving the framework did
    # real work rather than returning a constant.
    snapshot = aggregator.snapshot(symbol)
    assert snapshot.weighted_evidence, "no weighted evidence produced for the symbol"
    assert len(snapshot.weighted_evidence) == len(snapshot.active_evidence)
    weights = {w.weight for w in snapshot.weighted_evidence}
    assert all(0.0 <= w <= 1.0 for w in weights)
    assert len(weights) > 1, "every piece of evidence got the exact same weight -- the framework isn't discriminating"

    # -- /analyze incorporates all four dimensions at once, using the exact
    # same command plugin the Discord bot runs.
    analyze_plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus, settings=settings, plugin_config={},
            evidence_aggregator=aggregator, reasoning_engine=reasoning_engine,
            strategy_engine=strategy_engine, context_engine=context_engine,
        )
    )
    await analyze_plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": symbol})
    response = await analyze_plugin.execute(ctx)

    assert "insufficient_evidence" not in response.content
    assert "NVDA analysis" in response.content
    assert "Momentum Breakout" in response.content  # technical evidence -> strategy match
    assert "Bull Trend" in response.content  # market context
    assert "Confidence Weighting Framework" in response.content  # weighted confidence

    await registry.shutdown_all()


def test_context_engine_only_imports_generic_modules():
    """Architectural guarantee mirroring the Strategy/Scanner Engine's own
    checks: the Market Context Engine never imports the Evidence
    Aggregator, Strategy Engine, or Reasoning Engine directly -- everything
    it produces leaves only via the Event Bus."""
    import ast

    import app.context.engine as context_module

    allowed_prefixes = ("app.event_bus", "app.context", "app.logging")
    tree = ast.parse(Path(context_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            assert node.module.startswith(allowed_prefixes), (
                f"app.context.engine imports {node.module!r} -- the Market Context Engine "
                "must never import the Evidence Aggregator, Strategy Engine, or Reasoning Engine directly"
            )


def test_intelligence_plugin_base_only_imports_generic_modules():
    """Mirrors the Scanner Engine's structural guardrail: the shared
    IntelligencePlugin contract never imports a specific intelligence
    source's module -- concrete plugins (News/Earnings/Macro) depend on
    it, never the other way around."""
    import ast

    import app.intelligence.plugin as intelligence_module

    allowed_prefixes = ("app.event_bus", "app.evidence", "app.plugins", "app.logging", "app.intelligence")
    tree = ast.parse(Path(intelligence_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            assert node.module.startswith(allowed_prefixes), (
                f"app.intelligence.plugin imports {node.module!r} -- the shared intelligence "
                "plugin contract must never import a specific concrete intelligence plugin"
            )
