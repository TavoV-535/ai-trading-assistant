"""
Tests for the Portfolio Intelligence Layer's core engine
(app/portfolio/engine.py) — real event bus, crafted events, same pattern
tests/test_context_engine.py uses for the Market Context Engine.
"""
from __future__ import annotations

import asyncio

from app.event_bus.events import (
    AlertGenerated,
    EvidenceAggregated,
    MarketContextUpdated,
    StrategyMatched,
    SymbolProfileUpdated,
    WeightedEvidenceEvent,
)
from app.evidence.schema import Evidence, EvidenceCategory
from app.portfolio.engine import PortfolioIntelligenceEngine


def _tune_watchlist(settings, watchlist):
    settings.portfolio.watchlist = watchlist
    return settings


def _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish", category=EvidenceCategory.TREND, symbol="NVDA"):
    return Evidence(source=source, category=category, title=title, score=10, confidence=80, direction=direction, symbol=symbol)


async def _publish_evidence(event_bus, symbol, evidence, weight=0.5, active_evidence=None, has_conflict=False):
    active = active_evidence if active_evidence is not None else [evidence]
    await event_bus.publish(
        EvidenceAggregated(
            source="test",
            symbol=symbol,
            evidence=evidence,
            active_evidence=active,
            has_conflict=has_conflict,
            weighted_evidence=[WeightedEvidenceEvent(evidence=evidence, weight=weight, breakdown={})],
        )
    )


async def _collect_profile_updates(event_bus):
    seen = []
    event_bus.subscribe(SymbolProfileUpdated, lambda e: seen.append(e))
    return seen


async def test_only_configured_watchlist_symbols_are_tracked(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await _publish_evidence(event_bus, "AAPL", _evidence(symbol="AAPL"))
    await asyncio.sleep(0.05)

    assert engine.snapshot("AAPL") is None
    assert [p.symbol for p in engine.ranked_watchlist()] == ["NVDA"]


async def test_evidence_updates_counts_and_top_weight(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    bullish = _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish")
    bearish = _evidence(source="RSI", title="RSI Overbought", direction="bearish")
    await _publish_evidence(event_bus, "NVDA", bullish, weight=0.7, active_evidence=[bullish, bearish])
    await asyncio.sleep(0.05)

    profile = engine.snapshot("NVDA")
    assert profile.active_evidence_count == 2
    assert profile.bullish_count == 1
    assert profile.bearish_count == 1
    assert profile.top_weight == 0.7


async def test_fundamental_evidence_marks_freshness(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    settings.portfolio.fundamental_freshness_seconds = 600.0
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    news = _evidence(source="News", title="Earnings beat", category=EvidenceCategory.NEWS)
    await _publish_evidence(event_bus, "NVDA", news, weight=0.6)
    await asyncio.sleep(0.05)

    profile = engine.snapshot("NVDA")
    assert profile.has_fundamental_evidence is True
    assert profile.fundamental_evidence_fresh is True


async def test_technical_only_evidence_does_not_count_as_fundamental(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await _publish_evidence(event_bus, "NVDA", _evidence(category=EvidenceCategory.TREND), weight=0.6)
    await asyncio.sleep(0.05)

    profile = engine.snapshot("NVDA")
    assert profile.has_fundamental_evidence is False
    assert profile.fundamental_evidence_fresh is False


async def test_context_updated_populates_profile_context(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend"))
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA").context == {"trend": "Bull Trend"}


async def test_market_wide_context_is_not_attributed_to_any_symbol(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(MarketContextUpdated(source="test", symbol=None, context_type="risk_regime", label="Risk-Off"))
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA").context == {}


async def test_strategy_matched_appends_and_caps_at_five(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    for i in range(7):
        await event_bus.publish(StrategyMatched(source="test", strategy=f"Strategy {i}", symbol="NVDA", score=50))
    await asyncio.sleep(0.05)

    matched = engine.snapshot("NVDA").matched_strategies
    assert len(matched) == 5
    assert matched == [f"Strategy {i}" for i in range(2, 7)]  # oldest trimmed, most recent kept


async def test_alert_generated_updates_last_alert_and_count(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(
        AlertGenerated(
            source="test", symbol="NVDA", title="x", message="x", score=80.0, reason="x",
            source_event_type="StrategyMatched",
        )
    )
    await asyncio.sleep(0.05)

    profile = engine.snapshot("NVDA")
    assert profile.alert_count == 1
    assert profile.last_alert_at is not None


async def test_alert_generated_for_unrelated_symbol_is_ignored(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await event_bus.publish(
        AlertGenerated(
            source="test", symbol="AAPL", title="x", message="x", score=80.0, reason="x",
            source_event_type="StrategyMatched",
        )
    )
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA").alert_count == 0


async def test_confidence_trend_rises_with_increasing_average_weight(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    settings.portfolio.confidence_trend_window = 8
    settings.portfolio.confidence_trend_margin = 0.05
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    for weight in [0.1, 0.15, 0.2, 0.25, 0.6, 0.65, 0.7, 0.75]:
        ev = _evidence(title=f"signal at {weight}")
        await _publish_evidence(event_bus, "NVDA", ev, weight=weight)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA").confidence_trend == "rising"


async def test_confidence_trend_is_unknown_before_enough_history(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await _publish_evidence(event_bus, "NVDA", _evidence(), weight=0.5)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA").confidence_trend == "unknown"


async def test_symbol_profile_updated_is_edge_triggered(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)
    seen = await _collect_profile_updates(event_bus)

    # A strong new evidence item should cross the >=0.5 score-change threshold.
    await _publish_evidence(event_bus, "NVDA", _evidence(), weight=0.9)
    await asyncio.sleep(0.05)
    first_count = len(seen)
    assert first_count >= 1

    # Re-publishing evidence with a near-identical weight barely moves the
    # score -- must not spam another SymbolProfileUpdated.
    await _publish_evidence(event_bus, "NVDA", _evidence(title="Bullish EMA Cross"), weight=0.9)
    await asyncio.sleep(0.05)
    assert len(seen) == first_count


async def test_ranked_watchlist_sorts_by_priority_score_descending(event_bus, settings):
    _tune_watchlist(settings, ["NVDA", "AAPL", "TSLA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await _publish_evidence(event_bus, "NVDA", _evidence(symbol="NVDA"), weight=0.9)
    await _publish_evidence(event_bus, "AAPL", _evidence(symbol="AAPL"), weight=0.1)
    await asyncio.sleep(0.05)

    ranked = engine.ranked_watchlist()
    assert [p.symbol for p in ranked] == sorted([p.symbol for p in ranked], key=lambda s: -next(x.priority_score for x in ranked if x.symbol == s))
    assert ranked[0].symbol == "NVDA"  # strongest evidence -> highest priority
    assert "TSLA" in [p.symbol for p in ranked]  # untouched symbol still present, score 0


async def test_snapshot_returns_a_deep_copy_not_live_state(event_bus, settings):
    _tune_watchlist(settings, ["NVDA"])
    engine = PortfolioIntelligenceEngine(settings)
    engine.attach(event_bus)

    await _publish_evidence(event_bus, "NVDA", _evidence(), weight=0.5)
    await asyncio.sleep(0.05)

    snap = engine.snapshot("NVDA")
    snap.matched_strategies.append("mutated externally")
    assert "mutated externally" not in engine.snapshot("NVDA").matched_strategies
