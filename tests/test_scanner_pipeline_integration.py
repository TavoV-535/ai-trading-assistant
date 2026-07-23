"""
Milestone 6 completion requirement: prove the full continuous-scanning
pipeline end to end over the real Event Bus --

    ScannerPlugin -> MarketDataService -> ReplayProviderPlugin
        -> MarketDataUpdated -> real indicator plugins -> EvidenceProduced
        -> Evidence Aggregator -> Strategy Engine -> Reasoning Engine
        -> /analyze SYMBOL

-- with every MarketDataUpdated event coming from a real ScannerPlugin
ticking on a real asyncio background loop over real (compressed)
wall-clock time, reading from a real ReplayProviderPlugin replaying a CSV
file. Nothing here calls ``event_bus.publish(MarketDataUpdated(...))`` or
``event_bus.publish(EvidenceProduced(...))`` by hand -- that's the
difference from tests/test_pipeline_integration.py, which publishes the
same crafted bar sequence directly. This test proves the *scanning*
machinery itself, not just what happens once evidence exists.

Reuses the exact bar sequence tests/test_pipeline_integration.py already
validated produces a real "Momentum Breakout" match -- replayed through a
CSV file and a real Scanner tick loop instead of being published directly.
"""
from __future__ import annotations

import asyncio
import csv
import importlib.util
from pathlib import Path

from app.aggregation.aggregator import EvidenceAggregator
from app.discord.dispatch import CommandContext
from app.event_bus.events import EvidenceProduced, MarketDataUpdated
from app.marketdata.service import MarketDataService
from app.plugins import PluginRegistry
from app.plugins.base import PluginContext
from app.reasoning.engine import ReasoningEngine
from app.scanner.plugin import ScannerPlugin
from app.strategy.engine import StrategyEngine
from tests.test_pipeline_integration import _bars_for_momentum_breakout

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_class(rel_path: str, module_name: str, class_name: str):
    plugin_py = PROJECT_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


ReplayProviderPlugin = _load_plugin_class(
    "plugins/market_data/replay/plugin.py", "_test_scanner_pipeline_replay", "ReplayProviderPlugin"
)
AnalyzePlugin = _load_plugin_class(
    "plugins/commands/analyze/plugin.py", "_test_scanner_pipeline_analyze", "AnalyzePlugin"
)


def _write_csv(directory: Path, symbol: str, bars: list[dict]) -> None:
    csv_path = directory / f"{symbol}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["open", "high", "low", "close", "volume"])
        writer.writeheader()
        for bar in bars:
            writer.writerow(bar)


async def test_continuous_scanning_feeds_analyze_without_injected_evidence(event_bus, settings, tmp_path):
    bars = _bars_for_momentum_breakout()
    _write_csv(tmp_path, "NVDA", bars)

    # Real indicator plugins, discovered and loaded exactly like production --
    # restricted to just the indicators category since commands/market_data/
    # scanners are wired by hand below to control timing precisely.
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT, search_paths=["plugins/indicators"])

    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    strategy_engine = StrategyEngine(settings)
    strategy_engine.load(PROJECT_ROOT)
    strategy_engine.attach(event_bus)
    reasoning_engine = ReasoningEngine(settings, provider=None)
    reasoning_engine.attach(event_bus)

    # A real ReplayProviderPlugin reading the crafted CSV -- registered into
    # the same registry so MarketDataService discovers it the normal way.
    provider = ReplayProviderPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={"data_dir": str(tmp_path)})
    )
    await provider.initialize()
    registry._plugins[provider.name] = provider  # mirrors the injection pattern used elsewhere in this suite

    market_data_service = MarketDataService(settings, registry)
    assert [p.provider_name for p in market_data_service.providers] == ["replay"]

    # A real ScannerPlugin, ticking on a real background asyncio loop --
    # "continuous scanning using the real Event Bus," not a manual loop
    # publishing events by hand.
    scanner = ScannerPlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={"watchlist": ["NVDA"], "timeframes": ["1m"], "interval_seconds": 0.01},
            market_data_service=market_data_service,
        )
    )
    scanner.name = "TestReferenceScanner"

    market_updates: list[MarketDataUpdated] = []
    raw_evidence: list[EvidenceProduced] = []

    async def on_market_data(e: MarketDataUpdated) -> None:
        market_updates.append(e)

    async def on_evidence(e: EvidenceProduced) -> None:
        raw_evidence.append(e)

    event_bus.subscribe(MarketDataUpdated, on_market_data)
    event_bus.subscribe(EvidenceProduced, on_evidence)

    await scanner.initialize()
    # len(bars) ticks fully replays the CSV once; generous buffer for
    # scheduling jitter in a shared CI sandbox.
    await asyncio.sleep(len(bars) * 0.01 + 1.0)
    await scanner.shutdown()

    # 1. The scanner actually ticked through every bar via the real event
    # bus -- not a single hand-published event anywhere in this test.
    assert scanner._ticks >= len(bars)
    assert len(market_updates) >= len(bars)
    assert all(e.source == "TestReferenceScanner" for e in market_updates)

    # 2. Real indicator plugins reacted to that live data and published
    # real evidence -- nothing was injected directly.
    assert len(raw_evidence) > 0

    # 3. /analyze reflects that live-generated evidence -- not
    # "insufficient_evidence," and cites the real matched reference
    # strategy, using the exact same AnalyzePlugin the Discord bot runs.
    analyze_plugin = AnalyzePlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={},
            evidence_aggregator=aggregator,
            reasoning_engine=reasoning_engine,
            strategy_engine=strategy_engine,
        )
    )
    await analyze_plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": "nvda"})
    response = await analyze_plugin.execute(ctx)

    assert "insufficient_evidence" not in response.content
    assert "NVDA analysis" in response.content
    assert "Momentum Breakout" in response.content

    await registry.shutdown_all()
