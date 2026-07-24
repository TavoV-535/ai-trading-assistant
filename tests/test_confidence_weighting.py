"""
Tests for the Confidence Weighting Framework's math
(app/aggregation/weighting.py) — each factor exercised in isolation so a
regression in one doesn't hide behind the others.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.aggregation.models import EnrichmentInfo
from app.aggregation.weighting import DEFAULT_SOURCE_RELIABILITY, ConfidenceWeightingConfig, compute_weight
from app.evidence.schema import Evidence, EvidenceCategory


def _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish", category=EvidenceCategory.TREND, timeframe=None, symbol="NVDA"):
    return Evidence(
        source=source, category=category, title=title, score=10, confidence=80, direction=direction, symbol=symbol, timeframe=timeframe,
    )


def _enrichment(occurrence_count=1, freshness=1.0):
    now = datetime.now(timezone.utc)
    return EnrichmentInfo(
        group_key="x", occurrence_count=occurrence_count, is_duplicate=occurrence_count > 1,
        freshness=freshness, is_fresh=freshness > 0, first_seen_at=now, age_seconds=0.0,
    )


async def test_lone_fresh_first_time_evidence_uses_default_source_reliability_and_persistence_floor():
    evidence = _evidence()
    result = compute_weight(evidence, _enrichment(occurrence_count=1, freshness=1.0), [evidence], trend_label=None, config=ConfidenceWeightingConfig())

    assert result.breakdown["source_reliability"] == DEFAULT_SOURCE_RELIABILITY
    assert result.breakdown["freshness"] == 1.0
    assert result.breakdown["persistence"] == 0.5  # occurrence 1 -> floor
    assert result.breakdown["timeframe_alignment"] == 1.0  # no peers
    assert result.breakdown["cross_confirmation"] == 1.0  # no peers
    assert result.breakdown["contradiction"] == 1.0
    assert result.breakdown["regime_alignment"] == 1.0  # no trend context
    assert result.breakdown["correlation_dampening"] == 1.0  # alone in category
    assert result.weight == round(DEFAULT_SOURCE_RELIABILITY * 1.0 * 0.5, 4)


async def test_freshness_decays_the_weight():
    evidence = _evidence()
    fresh = compute_weight(evidence, _enrichment(freshness=1.0), [evidence], trend_label=None, config=ConfidenceWeightingConfig())
    stale = compute_weight(evidence, _enrichment(freshness=0.1), [evidence], trend_label=None, config=ConfidenceWeightingConfig())
    assert stale.weight < fresh.weight
    assert stale.breakdown["freshness"] == round(0.1 + 0.9 * 0.1, 4)


async def test_persistence_increases_with_occurrence_count_up_to_a_cap():
    evidence = _evidence()
    once = compute_weight(evidence, _enrichment(occurrence_count=1), [evidence], trend_label=None, config=ConfidenceWeightingConfig())
    five_times = compute_weight(evidence, _enrichment(occurrence_count=5), [evidence], trend_label=None, config=ConfidenceWeightingConfig())
    ten_times = compute_weight(evidence, _enrichment(occurrence_count=10), [evidence], trend_label=None, config=ConfidenceWeightingConfig())
    assert once.breakdown["persistence"] == 0.5
    assert five_times.breakdown["persistence"] == 1.0
    assert ten_times.breakdown["persistence"] == 1.0  # capped, not runaway


async def test_cross_confirmation_boosts_weight_for_agreeing_peers():
    # different category from the target so the correlation-dampening
    # factor (tested separately below) doesn't also kick in and confound
    # this assertion -- this test isolates cross-confirmation alone.
    target = _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish", category=EvidenceCategory.TREND)
    peer = _evidence(source="RSI", title="RSI Oversold", direction="bullish", category=EvidenceCategory.MOMENTUM)
    active = [target, peer]
    result = compute_weight(target, _enrichment(), active, trend_label=None, config=ConfidenceWeightingConfig())
    alone = compute_weight(target, _enrichment(), [target], trend_label=None, config=ConfidenceWeightingConfig())
    assert result.breakdown["cross_confirmation"] > alone.breakdown["cross_confirmation"]
    assert result.weight > alone.weight


async def test_contradiction_penalizes_weight():
    target = _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish")
    contrarian = _evidence(source="RSI", title="RSI Overbought", direction="bearish")
    active = [target, contrarian]
    result = compute_weight(target, _enrichment(), active, trend_label=None, config=ConfidenceWeightingConfig())
    alone = compute_weight(target, _enrichment(), [target], trend_label=None, config=ConfidenceWeightingConfig())
    assert result.breakdown["contradiction"] == ConfidenceWeightingConfig().contradiction_penalty
    assert result.weight < alone.weight


async def test_neutral_evidence_never_contradicts():
    target = _evidence(source="EMA", direction="bullish")
    neutral_peer = _evidence(source="RSI", title="RSI Neutral", direction="neutral")
    result = compute_weight(target, _enrichment(), [target, neutral_peer], trend_label=None, config=ConfidenceWeightingConfig())
    assert result.breakdown["contradiction"] == 1.0


async def test_regime_alignment_boosts_matching_direction_and_penalizes_opposing():
    bullish = _evidence(direction="bullish")
    bearish = _evidence(direction="bearish")
    config = ConfidenceWeightingConfig()

    aligned = compute_weight(bullish, _enrichment(), [bullish], trend_label="Bull Trend", config=config)
    opposed = compute_weight(bearish, _enrichment(), [bearish], trend_label="Bull Trend", config=config)
    neutral_context = compute_weight(bullish, _enrichment(), [bullish], trend_label=None, config=config)
    sideways = compute_weight(bullish, _enrichment(), [bullish], trend_label="Sideways Market", config=config)

    assert aligned.breakdown["regime_alignment"] == config.regime_aligned_boost
    assert opposed.breakdown["regime_alignment"] == config.regime_opposed_penalty
    assert neutral_context.breakdown["regime_alignment"] == 1.0
    assert sideways.breakdown["regime_alignment"] == 1.0
    assert aligned.weight > neutral_context.weight > opposed.weight


async def test_correlation_dampening_reduces_weight_for_same_category_cluster():
    a = _evidence(source="EMA", title="a", category=EvidenceCategory.TREND)
    b = _evidence(source="SMA", title="b", category=EvidenceCategory.TREND)
    c = _evidence(source="Supertrend", title="c", category=EvidenceCategory.TREND)
    active = [a, b, c]
    result = compute_weight(a, _enrichment(), active, trend_label=None, config=ConfidenceWeightingConfig())
    alone = compute_weight(a, _enrichment(), [a], trend_label=None, config=ConfidenceWeightingConfig())
    assert result.breakdown["correlation_dampening"] == round(1 / (3**0.5), 4)
    assert result.weight < alone.weight


async def test_timeframe_alignment_boosts_when_peers_share_timeframe():
    target = _evidence(timeframe="5m")
    peer_same = _evidence(source="RSI", title="x", timeframe="5m")
    peer_diff = _evidence(source="MACD", title="y", timeframe="1h")

    all_same = compute_weight(target, _enrichment(), [target, peer_same], trend_label=None, config=ConfidenceWeightingConfig())
    mixed = compute_weight(target, _enrichment(), [target, peer_same, peer_diff], trend_label=None, config=ConfidenceWeightingConfig())
    no_timeframe = compute_weight(_evidence(timeframe=None), _enrichment(), [_evidence(timeframe=None)], trend_label=None, config=ConfidenceWeightingConfig())

    assert all_same.breakdown["timeframe_alignment"] > mixed.breakdown["timeframe_alignment"] > 1.0
    assert no_timeframe.breakdown["timeframe_alignment"] == 1.0


async def test_source_reliability_config_override_is_used():
    config = ConfidenceWeightingConfig(source_reliability={"News": 0.4})
    news_evidence = _evidence(source="News", category=EvidenceCategory.NEWS)
    result = compute_weight(news_evidence, _enrichment(), [news_evidence], trend_label=None, config=config)
    assert result.breakdown["source_reliability"] == 0.4


async def test_weight_is_clamped_to_one_even_under_maximal_boosts():
    config = ConfidenceWeightingConfig(source_reliability={"EMA": 1.0})
    a = _evidence(source="EMA", title="a", direction="bullish", timeframe="5m")
    peers = [a] + [
        _evidence(source=f"Peer{i}", title=f"t{i}", direction="bullish", timeframe="5m", category=EvidenceCategory.MOMENTUM)
        for i in range(6)
    ]
    result = compute_weight(a, _enrichment(occurrence_count=10, freshness=1.0), peers, trend_label="Bull Trend", config=config)
    assert result.weight <= 1.0
    assert result.breakdown["final_weight"] <= 1.0


async def test_weight_never_goes_below_zero():
    config = ConfidenceWeightingConfig(source_reliability={"EMA": 0.0})
    a = _evidence(source="EMA")
    result = compute_weight(a, _enrichment(freshness=0.0), [a], trend_label=None, config=config)
    assert result.weight == 0.0


async def test_ml_adjustment_is_a_documented_neutral_seam():
    a = _evidence()
    result = compute_weight(a, _enrichment(), [a], trend_label=None, config=ConfidenceWeightingConfig())
    assert result.breakdown["ml_adjustment"] == 1.0


async def test_config_from_settings_reads_confidence_weighting_section(settings):
    settings.confidence_weighting.source_reliability = {"News": 0.5}
    settings.confidence_weighting.regime_aligned_boost = 1.5
    config = ConfidenceWeightingConfig.from_settings(settings)
    assert config.source_reliability == {"News": 0.5}
    assert config.regime_aligned_boost == 1.5


async def test_config_from_settings_handles_missing_section_gracefully():
    config = ConfidenceWeightingConfig.from_settings(object())
    assert config.source_reliability == {}
    assert config.regime_aligned_boost == 1.2
