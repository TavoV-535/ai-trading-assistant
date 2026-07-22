"""
Tests for the Strategy Engine (app/strategy/) — compilation, evaluation,
repeat-policy filtering, and edge-triggered ``StrategyMatched`` publishing.

Deliberately never imports or references a specific indicator plugin's
implementation — only ``Evidence`` objects, exactly as the Strategy Engine
itself is required to work (PROJECT.md: "The Strategy Engine should know
nothing about EMA, RSI, MACD, or any specific indicator implementation").
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from app.event_bus import EvidenceAggregated, StrategyMatched
from app.evidence import Evidence, EvidenceCategory
from app.strategy.compiler import compile_strategy
from app.strategy.engine import StrategyEngine
from app.strategy.loader import load_strategies
from app.strategy.models import StrategyDefinition

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _evidence(source, title, direction, score=10, confidence=70, symbol="NVDA", metadata=None):
    return Evidence(
        source=source,
        category=EvidenceCategory.TREND,
        title=title,
        score=score,
        confidence=confidence,
        direction=direction,
        symbol=symbol,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------- compiler


def test_compile_lowercases_and_freezes_titles():
    definition = StrategyDefinition(
        name="Test",
        required=["Bullish EMA Cross", " RSI Oversold "],
        optional=["CCI Breakout Above 100"],
        minimum_score=10,
    )
    compiled = compile_strategy(definition)
    assert compiled.required == frozenset({"bullish ema cross", "rsi oversold"})
    assert compiled.optional == frozenset({"cci breakout above 100"})


def test_evaluate_matches_when_required_present_and_score_met():
    compiled = compile_strategy(
        StrategyDefinition(name="Test", required=["Bullish EMA Cross"], minimum_score=10)
    )
    evaluation = compiled.evaluate([_evidence("EMA", "Bullish EMA Cross", "bullish", score=15)])
    assert evaluation.matched is True
    assert evaluation.score == 15
    assert evaluation.missing_required == []


def test_evaluate_does_not_match_when_required_missing():
    compiled = compile_strategy(
        StrategyDefinition(name="Test", required=["Bullish EMA Cross", "RSI Oversold"], minimum_score=0)
    )
    evaluation = compiled.evaluate([_evidence("EMA", "Bullish EMA Cross", "bullish", score=15)])
    assert evaluation.matched is False
    assert evaluation.missing_required == ["rsi oversold"]


def test_evaluate_does_not_match_when_score_below_minimum():
    compiled = compile_strategy(
        StrategyDefinition(name="Test", required=["Bullish EMA Cross"], minimum_score=100)
    )
    evaluation = compiled.evaluate([_evidence("EMA", "Bullish EMA Cross", "bullish", score=15)])
    assert evaluation.matched is False
    assert evaluation.score == 15


def test_optional_evidence_contributes_score_but_is_not_required():
    compiled = compile_strategy(
        StrategyDefinition(
            name="Test",
            required=["Bullish EMA Cross"],
            optional=["CCI Breakout Above 100"],
            minimum_score=20,
        )
    )
    # required alone (score 15) isn't enough
    low = compiled.evaluate([_evidence("EMA", "Bullish EMA Cross", "bullish", score=15)])
    assert low.matched is False

    # required + optional together clears the bar
    high = compiled.evaluate(
        [
            _evidence("EMA", "Bullish EMA Cross", "bullish", score=15),
            _evidence("CCI", "CCI Breakout Above 100", "bullish", score=7),
        ]
    )
    assert high.matched is True
    assert high.score == 22


def test_irrelevant_evidence_does_not_affect_score_or_match():
    compiled = compile_strategy(
        StrategyDefinition(name="Test", required=["Bullish EMA Cross"], minimum_score=10)
    )
    evaluation = compiled.evaluate(
        [
            _evidence("EMA", "Bullish EMA Cross", "bullish", score=15),
            _evidence("ATR", "Volatility Expansion (ATR)", "neutral", score=999),
        ]
    )
    assert evaluation.matched is True
    assert evaluation.score == 15  # the unrelated ATR evidence's score is not counted


# ---------------------------------------------------------------- repeat policy


def test_repeat_policy_every_breakout_accepts_all_occurrences():
    compiled = compile_strategy(
        StrategyDefinition(
            name="Test", required=["Donchian Channel Breakout (New High)"], minimum_score=0,
            repeat_policy={"Donchian": "every_breakout"},
        )
    )
    evaluation = compiled.evaluate(
        [_evidence("Donchian", "Donchian Channel Breakout (New High)", "bullish", metadata={"is_first_in_sequence": False})]
    )
    assert evaluation.matched is True


def test_repeat_policy_first_breakout_rejects_continuation():
    compiled = compile_strategy(
        StrategyDefinition(
            name="Test", required=["Donchian Channel Breakout (New High)"], minimum_score=0,
            repeat_policy={"Donchian": "first_breakout"},
        )
    )
    continuation = compiled.evaluate(
        [_evidence("Donchian", "Donchian Channel Breakout (New High)", "bullish", metadata={"is_first_in_sequence": False})]
    )
    assert continuation.matched is False

    first = compiled.evaluate(
        [_evidence("Donchian", "Donchian Channel Breakout (New High)", "bullish", metadata={"is_first_in_sequence": True, "is_first_ever": True})]
    )
    assert first.matched is True


def test_repeat_policy_after_pullback_rejects_cold_start_but_accepts_real_pullback():
    compiled = compile_strategy(
        StrategyDefinition(
            name="Test", required=["Donchian Channel Breakout (New High)"], minimum_score=0,
            repeat_policy={"Donchian": "after_pullback"},
        )
    )
    cold_start = compiled.evaluate(
        [_evidence("Donchian", "Donchian Channel Breakout (New High)", "bullish", metadata={"is_first_in_sequence": True, "is_first_ever": True})]
    )
    assert cold_start.matched is False  # first breakout ever -> no real pullback happened

    after_real_pullback = compiled.evaluate(
        [_evidence("Donchian", "Donchian Channel Breakout (New High)", "bullish", metadata={"is_first_in_sequence": True, "is_first_ever": False})]
    )
    assert after_real_pullback.matched is True


def test_repeat_policy_fails_open_when_evidence_lacks_sequence_metadata():
    """A source with a repeat_policy override that doesn't emit sequence
    metadata at all should never be silently excluded entirely."""
    compiled = compile_strategy(
        StrategyDefinition(
            name="Test", required=["Some Signal"], minimum_score=0,
            repeat_policy={"SomeSource": "after_pullback"},
        )
    )
    evaluation = compiled.evaluate([_evidence("SomeSource", "Some Signal", "bullish", metadata={})])
    assert evaluation.matched is True


def test_unknown_repeat_policy_fails_open():
    compiled = compile_strategy(
        StrategyDefinition(
            name="Test", required=["Some Signal"], minimum_score=0,
            repeat_policy={"SomeSource": "not_a_real_policy"},
        )
    )
    evaluation = compiled.evaluate([_evidence("SomeSource", "Some Signal", "bullish", metadata={"is_first_in_sequence": False})])
    assert evaluation.matched is True


# ---------------------------------------------------------------- loader


def test_loader_returns_empty_list_for_missing_directory(tmp_path):
    assert load_strategies(tmp_path / "does_not_exist") == []


def test_loader_skips_broken_strategy_file_without_crashing(tmp_path):
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    (good_dir / "strategy.yaml").write_text(yaml.dump({"name": "Good", "required": ["X"], "minimum_score": 5}))

    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    (broken_dir / "strategy.yaml").write_text("not: valid: yaml: [[[")

    compiled = load_strategies(tmp_path)
    names = [s.name for s in compiled]
    assert names == ["Good"]


def test_reference_momentum_breakout_strategy_loads_from_real_repo():
    compiled = load_strategies(PROJECT_ROOT / "plugins" / "strategies")
    names = [s.name for s in compiled]
    assert "Momentum Breakout" in names


# ---------------------------------------------------------------- engine (event-driven)


async def _load_engine(settings, strategies_dir: Path) -> StrategyEngine:
    engine = StrategyEngine(settings)
    engine._strategies = load_strategies(strategies_dir)  # test-only direct injection of a scoped strategy set
    return engine


async def test_strategy_matched_published_once_on_transition(event_bus, settings, tmp_path):
    strat_dir = tmp_path / "strategies" / "demo"
    strat_dir.mkdir(parents=True)
    (strat_dir / "strategy.yaml").write_text(
        yaml.dump({"name": "Demo Strategy", "required": ["Bullish EMA Cross"], "minimum_score": 10})
    )

    engine = await _load_engine(settings, tmp_path / "strategies")
    engine.attach(event_bus)

    matched_events = []

    async def on_matched(e: StrategyMatched) -> None:
        matched_events.append(e)

    event_bus.subscribe(StrategyMatched, on_matched)

    evidence = _evidence("EMA", "Bullish EMA Cross", "bullish", score=15)
    aggregated = EvidenceAggregated(source="EvidenceAggregator", symbol="NVDA", evidence=evidence, active_evidence=[evidence])

    # publish the SAME matching snapshot twice — must only fire once (edge-triggered)
    await event_bus.publish(aggregated)
    await event_bus.publish(aggregated)
    await asyncio.sleep(0.1)

    assert len(matched_events) == 1
    assert matched_events[0].strategy == "Demo Strategy"
    assert matched_events[0].symbol == "NVDA"
    assert matched_events[0].score == 15


async def test_strategy_matched_fires_again_after_unmatching_then_rematching(event_bus, settings, tmp_path):
    strat_dir = tmp_path / "strategies" / "demo"
    strat_dir.mkdir(parents=True)
    (strat_dir / "strategy.yaml").write_text(
        yaml.dump({"name": "Demo Strategy", "required": ["Bullish EMA Cross"], "minimum_score": 10})
    )

    engine = await _load_engine(settings, tmp_path / "strategies")
    engine.attach(event_bus)

    matched_events = []

    async def on_matched(e: StrategyMatched) -> None:
        matched_events.append(e)

    event_bus.subscribe(StrategyMatched, on_matched)

    evidence = _evidence("EMA", "Bullish EMA Cross", "bullish", score=15)
    matching = EvidenceAggregated(source="EvidenceAggregator", symbol="NVDA", evidence=evidence, active_evidence=[evidence])
    not_matching = EvidenceAggregated(source="EvidenceAggregator", symbol="NVDA", evidence=evidence, active_evidence=[])

    await event_bus.publish(matching)
    await event_bus.publish(not_matching)
    await event_bus.publish(matching)
    await asyncio.sleep(0.1)

    assert len(matched_events) == 2


async def test_strategy_engine_load_discovers_reference_strategy(settings):
    engine = StrategyEngine(settings)
    engine.load(PROJECT_ROOT)
    assert any(s.name == "Momentum Breakout" for s in engine.strategies)
