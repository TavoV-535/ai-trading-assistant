"""
Tests for the Event Prioritization Engine's core engine
(app/prioritization/engine.py) — real event bus, crafted candidate events,
verifying watchlist filtering, threshold gating, duplicate-suppression
cooldown, confidence-trend caching, and transparent decision history.
"""
from __future__ import annotations

import asyncio

from app.event_bus.events import AlertGenerated, EvidenceAggregated, MarketContextUpdated, StrategyMatched, SymbolProfileUpdated, WeightedEvidenceEvent
from app.evidence.schema import Evidence, EvidenceCategory
from app.prioritization.engine import EventPrioritizationEngine


def _tune(settings, watchlist=("NVDA",), **overrides):
    settings.portfolio.watchlist = list(watchlist)
    for key, value in overrides.items():
        setattr(settings.prioritization, key, value)
    return settings


async def _collect_alerts(event_bus):
    seen = []
    event_bus.subscribe(AlertGenerated, lambda e: seen.append(e))
    return seen


def _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish", symbol="NVDA", score=15):
    return Evidence(source=source, category=EvidenceCategory.TREND, title=title, score=score, confidence=80, direction=direction, symbol=symbol)


async def _publish_evidence(event_bus, symbol, evidence, weight, occurrence_count=1):
    await event_bus.publish(
        EvidenceAggregated(
            source="test",
            symbol=symbol,
            evidence=evidence,
            active_evidence=[evidence],
            enrichment={"occurrence_count": occurrence_count},
            weighted_evidence=[WeightedEvidenceEvent(evidence=evidence, weight=weight, breakdown={})],
        )
    )


async def test_symbol_not_on_watchlist_never_alerts_by_default(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="AAPL", score=90))
    await asyncio.sleep(0.05)

    assert alerts == []
    decisions = engine.decision_history("AAPL")
    assert decisions and decisions[-1].accepted is False
    assert "watchlist" in decisions[-1].reason


async def test_watchlist_only_false_allows_any_symbol(event_bus, settings):
    _tune(settings, watchlist=("NVDA",), watchlist_only=False)
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="AAPL", score=90))
    await asyncio.sleep(0.05)

    assert len(alerts) == 1
    assert alerts[0].symbol == "AAPL"


async def test_strategy_match_on_watchlist_clears_default_threshold(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="NVDA", score=90, evidence_count=2))
    await asyncio.sleep(0.05)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.symbol == "NVDA"
    assert alert.source_event_type == "StrategyMatched"
    assert alert.score >= settings.prioritization.alert_threshold
    assert alert.urgency in ("normal", "high", "critical")
    assert "Momentum Breakout" in alert.title


async def test_high_importance_context_shift_clears_threshold(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="gap", label="Gap Day (up)")
    )
    await asyncio.sleep(0.05)

    assert len(alerts) == 1
    assert alerts[0].source_event_type == "MarketContextUpdated"


async def test_routine_context_shift_with_no_confidence_trend_stays_below_threshold(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend")
    )
    await asyncio.sleep(0.05)

    assert alerts == []
    decisions = engine.decision_history("NVDA")
    assert decisions[-1].accepted is False
    assert decisions[-1].reason == "below alert threshold"


async def test_confidence_trend_is_cached_from_symbol_profile_updated_and_boosts_score(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(
        SymbolProfileUpdated(source="test", symbol="NVDA", priority_score=50.0, confidence_trend="rising")
    )
    await asyncio.sleep(0.02)

    # The same routine context shift that stayed below threshold with an
    # "unknown" trend should now clear it once "rising" is cached.
    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend")
    )
    await asyncio.sleep(0.05)

    assert len(alerts) == 1


async def test_duplicate_within_cooldown_is_suppressed(event_bus, settings):
    _tune(settings, watchlist=("NVDA",), alert_cooldown_seconds=300.0)
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    for _ in range(2):
        await event_bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="NVDA", score=90))
    await asyncio.sleep(0.05)

    assert len(alerts) == 1
    decisions = engine.decision_history("NVDA")
    assert any(d.reason == "duplicate suppressed (within cooldown)" for d in decisions)


async def test_different_alert_key_is_not_suppressed_by_another_keys_cooldown(event_bus, settings):
    _tune(settings, watchlist=("NVDA",), alert_cooldown_seconds=300.0)
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="NVDA", score=90))
    await event_bus.publish(MarketContextUpdated(source="test", symbol="NVDA", context_type="gap", label="Gap Day"))
    await asyncio.sleep(0.05)

    assert len(alerts) == 2


async def test_fresh_strong_evidence_can_clear_threshold(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    strong = _evidence(title="Strong breakout signal", score=29)
    await _publish_evidence(event_bus, "NVDA", strong, weight=1.0, occurrence_count=1)
    await asyncio.sleep(0.05)

    assert len(alerts) == 1
    assert alerts[0].source_event_type == "EvidenceAggregated"


async def test_weak_repeated_evidence_does_not_clear_threshold(event_bus, settings):
    _tune(settings, watchlist=("NVDA",))
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    weak = _evidence(title="Minor wobble", score=3)
    await _publish_evidence(event_bus, "NVDA", weak, weight=0.2, occurrence_count=5)
    await asyncio.sleep(0.05)

    assert alerts == []


async def test_decision_history_is_bounded_by_decision_log_size(event_bus, settings):
    _tune(settings, watchlist=("NVDA",), decision_log_size=3, alert_cooldown_seconds=0.0)
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)

    for i in range(10):
        await event_bus.publish(MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label=f"Bull Trend {i}"))
    await asyncio.sleep(0.1)

    assert len(engine.decision_history("NVDA")) == 3


async def test_market_wide_candidate_uses_none_symbol_bucket(event_bus, settings):
    _tune(settings, watchlist=("NVDA",), watchlist_only=False)
    engine = EventPrioritizationEngine(settings)
    engine.attach(event_bus)
    alerts = await _collect_alerts(event_bus)

    await event_bus.publish(MarketContextUpdated(source="test", symbol=None, context_type="risk_regime", label="Risk-Off"))
    await asyncio.sleep(0.05)

    assert len(alerts) == 1
    assert alerts[0].symbol is None
    assert engine.decision_history(None)
