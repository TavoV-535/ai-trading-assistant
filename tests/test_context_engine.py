"""
Tests for the Market Context Engine (app/context/engine.py) — trend,
volatility, gap, exhaustion, liquidity, market-wide risk regime, and
macro-event-driven context, all derived from real MarketDataUpdated /
EvidenceProduced events on a real event bus.
"""
from __future__ import annotations

import asyncio

from app.context.engine import MarketContextEngine
from app.event_bus.events import EvidenceProduced, MarketContextUpdated, MarketDataUpdated
from app.evidence.schema import Evidence, EvidenceCategory


def _tune(settings, **overrides):
    for key, value in overrides.items():
        setattr(settings.context, key, value)
    return settings


async def _publish_bar(event_bus, symbol, price, volume=None):
    await event_bus.publish(MarketDataUpdated(source="test", symbol=symbol, price=price, close=price, volume=volume))


async def _collect(event_bus):
    seen: list[MarketContextUpdated] = []
    event_bus.subscribe(MarketContextUpdated, lambda e: seen.append(e))
    return seen


async def test_bull_trend_detected_from_rising_prices(event_bus, settings):
    _tune(settings, trend_window=6, trend_bull_threshold_pct=2.0, trend_bear_threshold_pct=-2.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    for price in [100, 101, 102, 103, 104]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    trend_events = [e for e in seen if e.context_type == "trend"]
    assert trend_events
    assert trend_events[-1].label == "Bull Trend"
    assert trend_events[-1].symbol == "NVDA"
    assert engine.snapshot("NVDA")["trend"] == "Bull Trend"


async def test_bear_trend_detected_from_falling_prices(event_bus, settings):
    _tune(settings, trend_window=6, trend_bull_threshold_pct=2.0, trend_bear_threshold_pct=-2.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for price in [100, 99, 98, 97, 96]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["trend"] == "Bear Trend"


async def test_sideways_market_when_price_barely_moves(event_bus, settings):
    _tune(settings, trend_window=6, trend_bull_threshold_pct=2.0, trend_bear_threshold_pct=-2.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for price in [100, 100.1, 99.9, 100.2, 100.0]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["trend"] == "Sideways Market"


async def test_trend_is_edge_triggered_not_republished_every_tick(event_bus, settings):
    _tune(settings, trend_window=6, trend_bull_threshold_pct=2.0, trend_bear_threshold_pct=-2.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    for price in [100, 101, 102, 103, 104, 105, 106, 107]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    trend_events = [e for e in seen if e.context_type == "trend"]
    # every one of these publishes is still "Bull Trend" -- edge-triggering
    # means it only actually got published once.
    assert len(trend_events) == 1


async def test_high_volatility_detected_from_choppy_prices(event_bus, settings):
    _tune(settings, volatility_window=6, high_volatility_threshold_pct=1.0, low_volatility_threshold_pct=0.05)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for price in [100, 105, 98, 107, 95, 110]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["volatility"] == "High Volatility"


async def test_low_volatility_detected_from_flat_prices(event_bus, settings):
    _tune(settings, volatility_window=6, high_volatility_threshold_pct=1.0, low_volatility_threshold_pct=0.05)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for price in [100.0, 100.01, 100.0, 99.99, 100.0]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["volatility"] == "Low Volatility"


async def test_gap_day_detected_on_large_jump_between_updates(event_bus, settings):
    _tune(settings, gap_threshold_pct=3.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    await _publish_bar(event_bus, "NVDA", 100)
    await _publish_bar(event_bus, "NVDA", 106)  # +6% jump
    await asyncio.sleep(0.05)

    gap_events = [e for e in seen if e.context_type == "gap"]
    assert gap_events
    assert gap_events[-1].label == "Gap Day"
    assert gap_events[-1].metadata["change_percent"] > 3.0


async def test_no_gap_for_small_moves(event_bus, settings):
    _tune(settings, gap_threshold_pct=3.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    await _publish_bar(event_bus, "NVDA", 100)
    await _publish_bar(event_bus, "NVDA", 100.5)
    await asyncio.sleep(0.05)

    assert not [e for e in seen if e.context_type == "gap"]


async def test_trend_exhaustion_detected_on_decelerating_bull_trend(event_bus, settings):
    _tune(settings, trend_window=8, trend_bull_threshold_pct=2.0, trend_bear_threshold_pct=-2.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    # strong first half (100 -> 120, +20%), decelerating second half (120 -> 121, ~+0.8%)
    for price in [100, 105, 110, 115, 120, 120.2, 120.5, 121]:
        await _publish_bar(event_bus, "NVDA", price)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["trend"] == "Bull Trend"
    assert engine.snapshot("NVDA")["exhaustion"] == "Trend Exhaustion"


async def test_low_liquidity_detected_from_declining_volume(event_bus, settings):
    _tune(settings, low_liquidity_volume_ratio=0.5)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for volume in [1000, 1000, 1000, 1000, 200]:
        await _publish_bar(event_bus, "NVDA", 100, volume=volume)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["liquidity"] == "Low Liquidity"


async def test_risk_on_published_when_majority_of_symbols_bull_trend(event_bus, settings):
    _tune(
        settings,
        trend_window=6,
        trend_bull_threshold_pct=2.0,
        trend_bear_threshold_pct=-2.0,
        risk_regime_min_symbols=2,
        risk_regime_majority_ratio=0.6,
    )
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for symbol in ["NVDA", "AAPL", "TSLA"]:
        for price in [100, 101, 102, 103, 104]:
            await _publish_bar(event_bus, symbol, price)
    await asyncio.sleep(0.05)

    assert engine.snapshot(None)["risk_regime"] == "Risk-On"


async def test_risk_off_published_when_majority_of_symbols_bear_trend(event_bus, settings):
    _tune(
        settings,
        trend_window=6,
        trend_bull_threshold_pct=2.0,
        trend_bear_threshold_pct=-2.0,
        risk_regime_min_symbols=2,
        risk_regime_majority_ratio=0.6,
    )
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for symbol in ["NVDA", "AAPL", "TSLA"]:
        for price in [100, 99, 98, 97, 96]:
            await _publish_bar(event_bus, symbol, price)
    await asyncio.sleep(0.05)

    assert engine.snapshot(None)["risk_regime"] == "Risk-Off"


async def test_macro_context_hint_promoted_from_intelligence_evidence(event_bus, settings):
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="Macro",
            evidence=Evidence(
                source="Macro",
                category=EvidenceCategory.MACRO,
                title="Federal Reserve FOMC meeting this week",
                score=20,
                confidence=80,
                direction="neutral",
                symbol=None,
                metadata={"event_type": "fed_meeting", "context_hint": "fed_week"},
            ),
        )
    )
    await asyncio.sleep(0.05)

    macro_events = [e for e in seen if e.context_type == "macro_event"]
    assert macro_events
    assert macro_events[-1].label == "Fed Week"
    assert macro_events[-1].symbol is None
    assert engine.snapshot(None)["macro_event"] == "Fed Week"


async def test_unknown_context_hint_falls_back_to_title_case(event_bus, settings):
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="Macro",
            evidence=Evidence(
                source="Macro",
                category=EvidenceCategory.MACRO,
                title="Something new",
                score=10,
                confidence=50,
                direction="neutral",
                metadata={"context_hint": "surprise_rate_hike"},
            ),
        )
    )
    await asyncio.sleep(0.05)

    macro_events = [e for e in seen if e.context_type == "macro_event"]
    assert macro_events[-1].label == "Surprise Rate Hike"


async def test_evidence_without_context_hint_is_ignored(event_bus, settings):
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)
    seen = await _collect(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(
                source="EMA", category=EvidenceCategory.TREND, title="Bullish EMA Cross",
                score=15, confidence=80, direction="bullish", symbol="NVDA",
            ),
        )
    )
    await asyncio.sleep(0.05)

    assert not [e for e in seen if e.context_type == "macro_event"]


async def test_snapshot_for_unknown_symbol_is_empty(settings):
    engine = MarketContextEngine(settings)
    assert engine.snapshot("NOTHING_EVER_PUBLISHED") == {}


async def test_symbols_are_isolated_from_each_other(event_bus, settings):
    _tune(settings, trend_window=6, trend_bull_threshold_pct=2.0, trend_bear_threshold_pct=-2.0)
    engine = MarketContextEngine(settings)
    engine.attach(event_bus)

    for price in [100, 101, 102, 103, 104]:
        await _publish_bar(event_bus, "NVDA", price)
    for price in [100, 99, 98, 97, 96]:
        await _publish_bar(event_bus, "AAPL", price)
    await asyncio.sleep(0.05)

    assert engine.snapshot("NVDA")["trend"] == "Bull Trend"
    assert engine.snapshot("AAPL")["trend"] == "Bear Trend"
