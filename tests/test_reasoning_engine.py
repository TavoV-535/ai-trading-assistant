from __future__ import annotations

import asyncio
import json

from app.aggregation.aggregator import EvidenceAggregator
from app.evidence import Evidence, EvidenceCategory
from app.event_bus import EvidenceProduced, MarketContextUpdated
from app.reasoning import ReasoningEngine
from app.reasoning.providers.base import ReasoningProvider


def _attach_aggregator(settings, event_bus) -> EvidenceAggregator:
    """The Reasoning Engine now consumes EvidenceAggregated exclusively
    (never raw EvidenceProduced directly) — every test that publishes
    evidence needs a live aggregator on the same bus to bridge the two,
    exactly like a real deployment's bootstrap wires them."""
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)
    return aggregator


class FakeProvider(ReasoningProvider):
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.last_system: str | None = None
        self.last_prompt: str | None = None

    async def generate(self, *, system, prompt, max_tokens, temperature):
        self.last_system = system
        self.last_prompt = prompt
        if self._error:
            raise self._error
        return json.dumps(self._response)


async def test_insufficient_evidence_returns_zero_confidence(settings):
    engine = ReasoningEngine(settings, provider=None)
    out = await engine.analyze("NVDA")
    assert out.source == "insufficient_evidence"
    assert out.confidence == 0


async def test_evidence_only_fallback_without_provider(settings, event_bus):
    _attach_aggregator(settings, event_bus)
    engine = ReasoningEngine(settings, provider=None)
    engine.attach(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(
                source="EMA",
                category=EvidenceCategory.TREND,
                title="Bullish EMA Cross",
                score=15,
                confidence=91,
                direction="bullish",
                symbol="NVDA",
            ),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("NVDA")
    assert out.source == "evidence_only"
    assert out.evidence_count == 1
    assert out.confidence == 91


async def test_ai_path_uses_provider_and_includes_mission_statement_in_system_prompt(settings, event_bus):
    provider = FakeProvider(
        response={
            "market_summary": "summary",
            "trade_thesis": "thesis",
            "risk_assessment": "risk",
            "alternative_scenario": "alt",
            "confidence": 82,
            "suggested_strategies": ["Momentum"],
            "historical_similarity": "similar to March",
        }
    )
    _attach_aggregator(settings, event_bus)
    engine = ReasoningEngine(settings, provider=provider)
    engine.attach(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="RelVol",
            evidence=Evidence(source="RelVol", category="Volume", title="2x volume", score=10, confidence=85, direction="bullish", symbol="NVDA"),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("NVDA")
    assert out.source == "ai"
    assert out.confidence == 82
    assert out.suggested_strategies == ["Momentum"]
    assert "signal-selling" in provider.last_system.lower()
    assert "NVDA" in provider.last_prompt


async def test_ai_failure_falls_back_to_evidence_only(settings, event_bus):
    _attach_aggregator(settings, event_bus)
    provider = FakeProvider(error=RuntimeError("simulated network failure"))
    engine = ReasoningEngine(settings, provider=provider)
    engine.attach(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(source="EMA", category="Trend", title="x", score=5, confidence=50, direction="neutral", symbol="TSLA"),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("TSLA")
    assert out.source == "evidence_only"


async def test_reasoning_disabled_in_config_uses_evidence_only(settings, event_bus):
    settings.reasoning.enabled = False
    _attach_aggregator(settings, event_bus)
    provider = FakeProvider(response={
        "market_summary": "x", "trade_thesis": "x", "risk_assessment": "x",
        "alternative_scenario": "x", "confidence": 50,
    })
    engine = ReasoningEngine(settings, provider=provider)
    engine.attach(event_bus)

    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(source="EMA", category="Trend", title="x", score=5, confidence=50, direction="neutral", symbol="AAPL"),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("AAPL")
    assert out.source == "evidence_only"
    assert provider.last_system is None  # provider was never called


# ---------------------------------------------------------------- Milestone 7: context + weighted confidence


async def test_evidence_only_summary_includes_current_market_context(settings, event_bus):
    _attach_aggregator(settings, event_bus)
    engine = ReasoningEngine(settings, provider=None)
    engine.attach(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend")
    )
    await event_bus.publish(
        MarketContextUpdated(source="test", symbol=None, context_type="risk_regime", label="Risk-On")
    )
    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(source="EMA", category=EvidenceCategory.TREND, title="Bullish EMA Cross", score=15, confidence=91, direction="bullish", symbol="NVDA"),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("NVDA")
    assert out.context == {"trend": "Bull Trend", "risk_regime": "Risk-On"}
    assert "Bull Trend" in out.market_summary
    assert "Risk-On" in out.market_summary


async def test_market_wide_context_does_not_leak_into_a_different_symbol_specific_bucket(settings, event_bus):
    _attach_aggregator(settings, event_bus)
    engine = ReasoningEngine(settings, provider=None)
    engine.attach(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend")
    )
    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(source="EMA", category=EvidenceCategory.TREND, title="x", score=5, confidence=50, direction="neutral", symbol="AAPL"),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("AAPL")
    assert "trend" not in out.context  # NVDA's trend label doesn't apply to AAPL


async def test_insufficient_evidence_still_reports_known_context(settings, event_bus):
    engine = ReasoningEngine(settings, provider=None)
    engine.attach(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol=None, context_type="risk_regime", label="Risk-Off")
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("GHOST")
    assert out.source == "insufficient_evidence"
    assert out.context == {"risk_regime": "Risk-Off"}


async def test_ai_prompt_includes_context_and_confidence_weight(settings, event_bus):
    provider = FakeProvider(
        response={
            "market_summary": "s", "trade_thesis": "t", "risk_assessment": "r",
            "alternative_scenario": "a", "confidence": 70,
        }
    )
    _attach_aggregator(settings, event_bus)
    engine = ReasoningEngine(settings, provider=provider)
    engine.attach(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend")
    )
    await event_bus.publish(
        EvidenceProduced(
            source="EMA",
            evidence=Evidence(source="EMA", category=EvidenceCategory.TREND, title="Bullish EMA Cross", score=15, confidence=80, direction="bullish", symbol="NVDA"),
        )
    )
    await asyncio.sleep(0.05)

    out = await engine.analyze("NVDA")
    assert out.context == {"trend": "Bull Trend"}
    assert "confidence_weight" in provider.last_prompt
    assert "Bull Trend" in provider.last_prompt


async def test_evidence_only_summary_uses_weighted_mass_for_lean_when_available(settings, event_bus):
    """A single, highly-confirmed-and-aligned bullish signal should be able
    to out-weigh a larger pile of low-weight bearish noise once the
    Confidence Weighting Framework is in play."""
    aggregator = _attach_aggregator(settings, event_bus)
    engine = ReasoningEngine(settings, provider=None)
    engine.attach(event_bus)

    await event_bus.publish(
        MarketContextUpdated(source="test", symbol="NVDA", context_type="trend", label="Bull Trend")
    )
    await asyncio.sleep(0.02)

    # one aligned, persistent bullish signal (fires 5x so persistence maxes out)
    for _ in range(5):
        await event_bus.publish(
            EvidenceProduced(
                source="EMA",
                evidence=Evidence(source="EMA", category=EvidenceCategory.TREND, title="Bullish EMA Cross", score=10, confidence=80, direction="bullish", symbol="NVDA"),
            )
        )
    await asyncio.sleep(0.05)

    snap = aggregator.snapshot("NVDA")
    assert snap.weighted_evidence[0].weight > 0.5  # aligned + persistent + fresh

    out = await engine.analyze("NVDA")
    assert out.source == "evidence_only"
    assert "bullish" in out.market_summary.lower()
