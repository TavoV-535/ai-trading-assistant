"""
End-to-end integration test for the Milestone 4 pipeline:

    Indicator Plugins -> EvidenceProduced -> Evidence Aggregator
        -> EvidenceAggregated -> Strategy Engine -> StrategyMatched
        -> Reasoning Engine -> educational, non-directive synthesis

Boots the real plugin registry, a real Evidence Aggregator, and a real
Strategy Engine loading the actual `plugins/strategies/momentum_breakout`
reference strategy from the repo — the same wiring `app.core.bootstrap`
uses, not a simplified stand-in.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.aggregation.aggregator import EvidenceAggregator
from app.event_bus import EvidenceAggregated, EvidenceProduced, MarketDataUpdated, StrategyMatched
from app.plugins import PluginRegistry
from app.reasoning.engine import ReasoningEngine
from app.strategy.engine import StrategyEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _publish_bars(event_bus, symbol, bars, timeframe="1m"):
    for bar in bars:
        await event_bus.publish(
            MarketDataUpdated(
                symbol=symbol,
                price=bar["close"],
                open=bar.get("open"),
                high=bar.get("high"),
                low=bar.get("low"),
                close=bar.get("close"),
                volume=bar.get("volume"),
                timeframe=timeframe,
            )
        )
    await asyncio.sleep(0.2)


def _ramp(base: float, n: int, volume: float = 500) -> list[dict]:
    """No upper wick (high == close): each bar's close must itself exceed
    the *prior* bar's high for Donchian to register a fresh channel
    breakout — a wick above the close would let the prior bar's high
    outrun the next bar's close and stall the streak after one bar (see
    the same fix in test_donchian_repeat_policy.py)."""
    return [{"open": base + i - 1, "high": base + i, "low": base + i - 3, "close": base + i, "volume": volume} for i in range(1, n + 1)]


def _bars_for_momentum_breakout():
    """Crafted so the real EMA, SMA, CCI, and Donchian plugins all publish
    the exact evidence titles `plugins/strategies/momentum_breakout`
    requires/optionally-uses, AND so Donchian gets a genuine second
    breakout wave following a real pullback — required because the
    reference strategy's own `repeat_policy: {Donchian: after_pullback}`
    excludes a cold-start first breakout (there's no real pullback behind
    it, see plugins/indicators/donchian/plugin.py):

    1. long flat warmup — lets the slow moving averages / Ichimoku / ADX
       fully settle before anything moves
    2. a first breakout wave — flips EMA/SMA/MACD/CCI/ADX/Bollinger/
       Ichimoku/Supertrend/OBV bullish, and gives Donchian a first breakout
       (excluded by this strategy's after_pullback policy, but it's what
       makes the *next* wave a genuine "after a pullback" breakout)
    3. a brief, shallow pullback — no new Donchian highs (streak resets),
       shallow enough that the 20/50-period moving averages don't reverse
    4. a second breakout wave — Donchian's first breakout in this new
       sequence now satisfies after_pullback
    """
    flat = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 500} for _ in range(60)]
    wave1 = _ramp(100, 15)  # closes 101..115 -> EMA/SMA/CCI/... all flip bullish during this wave
    pullback = [{"open": 112, "high": 113, "low": 108, "close": 110, "volume": 500} for _ in range(3)]  # stays below 115
    # A single, decisive breakout bar — not a continued ramp. The Evidence
    # Aggregator dedupes by (source, title), keeping only the *latest*
    # occurrence as the "active" representative for that group. Donchian
    # fires on every bar of a sustained trend, so if wave 2 kept climbing,
    # each new bar's evidence would immediately supersede the previous one
    # in the active snapshot — including the one qualifying occurrence this
    # strategy's `after_pullback` policy is looking for. A single bar here
    # means that qualifying occurrence is also the *last* one, so it's
    # still the active representative when the strategy re-evaluates.
    wave2 = [{"open": 120, "high": 125, "low": 119, "close": 125, "volume": 500}]
    return flat + wave1 + pullback + wave2


async def test_full_pipeline_from_indicators_to_reasoning_engine(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)

    strategy_engine = StrategyEngine(settings)
    strategy_engine.load(PROJECT_ROOT)
    assert any(s.name == "Momentum Breakout" for s in strategy_engine.strategies)
    strategy_engine.attach(event_bus)

    reasoning = ReasoningEngine(settings, provider=None)  # evidence-only mode, no API key needed
    reasoning.attach(event_bus)

    raw_evidence: list[EvidenceProduced] = []
    aggregated_events: list[EvidenceAggregated] = []
    strategy_matches: list[StrategyMatched] = []

    async def on_raw(e: EvidenceProduced) -> None:
        raw_evidence.append(e)

    async def on_aggregated(e: EvidenceAggregated) -> None:
        aggregated_events.append(e)

    async def on_matched(e: StrategyMatched) -> None:
        strategy_matches.append(e)

    event_bus.subscribe(EvidenceProduced, on_raw)
    event_bus.subscribe(EvidenceAggregated, on_aggregated)
    event_bus.subscribe(StrategyMatched, on_matched)

    symbol = "PIPELINE"
    await _publish_bars(event_bus, symbol, _bars_for_momentum_breakout())

    # 1. Indicator plugins actually published raw evidence
    assert len(raw_evidence) > 0, "no indicator plugin published any evidence at all"

    # 2. The aggregator processed every single one of them (1:1)
    assert len(aggregated_events) == len(raw_evidence)
    last_aggregated = aggregated_events[-1]
    assert last_aggregated.symbol == symbol
    assert len(last_aggregated.active_evidence) > 0

    # 3. The Strategy Engine matched the real, declarative reference
    # strategy from plugins/strategies/momentum_breakout/strategy.yaml —
    # not a hand-constructed test strategy.
    assert len(strategy_matches) >= 1
    assert any(m.strategy == "Momentum Breakout" for m in strategy_matches)
    match = next(m for m in strategy_matches if m.strategy == "Momentum Breakout")
    assert match.symbol == symbol
    assert match.score >= 32  # the strategy's minimum_score

    # 4. The Reasoning Engine received the aggregator's output (not raw
    # EvidenceProduced) and its synthesis mentions the matched strategy.
    output = await reasoning.analyze(symbol)
    assert output.source == "evidence_only"
    assert output.evidence_count > 0
    assert "Momentum Breakout" in output.suggested_strategies
    assert "Momentum Breakout" in output.market_summary

    # 5. Never a directive — every piece of evidence anywhere in this run
    # stayed within the Universal Evidence Object's vocabulary.
    for event in raw_evidence:
        assert event.evidence.direction in ("bullish", "bearish", "neutral")
        assert not any(word in event.evidence.title.lower() for word in ("buy", "sell", "should"))

    await registry.shutdown_all()


def test_strategy_engine_only_imports_generic_modules():
    """Architectural guarantee, checked structurally rather than by
    grepping prose (docstrings legitimately *discuss* EMA/RSI/MACD as
    examples — that's not a violation): the Strategy Engine's actual
    Python ``import`` statements never name a specific indicator plugin
    module. It only ever imports the Event Bus, its own compiler/loader/
    models, logging, and the standard library."""
    import ast

    import app.strategy.engine as engine_module
    import app.strategy.compiler as compiler_module

    allowed_prefixes = ("app.event_bus", "app.strategy", "app.logging", "app.evidence")
    for module in (engine_module, compiler_module):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                assert node.module.startswith(allowed_prefixes), (
                    f"{module.__name__} imports {node.module!r} — the Strategy Engine "
                    "must never import a specific indicator/plugin module"
                )
