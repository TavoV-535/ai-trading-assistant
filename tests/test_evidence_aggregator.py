"""
Tests for the Evidence Aggregator (app/aggregation/) — the layer between
every evidence producer and both the Strategy Engine and Reasoning Engine.
Covers deduplication, freshness/decay, conflict detection, and that the
full historical sequence is never discarded even when the "active" view
collapses repeats.
"""
from __future__ import annotations

import asyncio

from app.aggregation.aggregator import EvidenceAggregator
from app.event_bus import EvidenceAggregated
from app.evidence import Evidence, EvidenceCategory


def _evidence(source="EMA", title="Bullish EMA Cross", direction="bullish", symbol="NVDA", score=15, confidence=80):
    return Evidence(
        source=source,
        category=EvidenceCategory.TREND,
        title=title,
        score=score,
        confidence=confidence,
        direction=direction,
        symbol=symbol,
    )


async def _publish_evidence(event_bus, source, title, direction, symbol="NVDA", score=10, confidence=70):
    from app.event_bus import EvidenceProduced

    await event_bus.publish(
        EvidenceProduced(source=source, evidence=_evidence(source, title, direction, symbol, score, confidence))
    )


async def test_single_evidence_produces_one_aggregated_event(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)

    seen = []

    async def on_aggregated(e: EvidenceAggregated) -> None:
        seen.append(e)

    event_bus.subscribe(EvidenceAggregated, on_aggregated)

    await _publish_evidence(event_bus, "EMA", "Bullish EMA Cross", "bullish")
    await asyncio.sleep(0.05)

    assert len(seen) == 1
    assert seen[0].symbol == "NVDA"
    assert seen[0].evidence.title == "Bullish EMA Cross"
    assert seen[0].active_evidence == [seen[0].evidence]
    assert seen[0].has_conflict is False


async def test_duplicate_evidence_is_deduped_in_active_snapshot_but_counted(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)

    seen = []

    async def on_aggregated(e: EvidenceAggregated) -> None:
        seen.append(e)

    event_bus.subscribe(EvidenceAggregated, on_aggregated)

    # same source+title fires three times in a row (a repeated confirmation)
    for _ in range(3):
        await _publish_evidence(event_bus, "RSI", "RSI Overbought", "bearish")
    await asyncio.sleep(0.05)

    assert len(seen) == 3
    last = seen[-1]
    # deduped: only one representative for this group in the active snapshot
    assert len(last.active_evidence) == 1
    assert last.enrichment["occurrence_count"] == 3
    assert last.enrichment["is_duplicate"] is True

    # nothing was thrown away — full history still has all 3
    assert len(aggregator.history("NVDA")) == 3


async def test_different_sources_both_present_in_active_snapshot(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)

    await _publish_evidence(event_bus, "EMA", "Bullish EMA Cross", "bullish")
    await _publish_evidence(event_bus, "RSI", "RSI Oversold", "bullish")
    await asyncio.sleep(0.05)

    snap = aggregator.snapshot("NVDA")
    assert len(snap.active_evidence) == 2
    assert snap.bullish_count == 2
    assert snap.bearish_count == 0
    assert snap.has_conflict is False


async def test_conflicting_directions_flagged(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)

    await _publish_evidence(event_bus, "EMA", "Bullish EMA Cross", "bullish")
    await _publish_evidence(event_bus, "RSI", "RSI Overbought", "bearish")
    await asyncio.sleep(0.05)

    snap = aggregator.snapshot("NVDA")
    assert snap.has_conflict is True
    assert snap.bullish_count == 1
    assert snap.bearish_count == 1


async def test_stale_evidence_decays_out_of_active_snapshot(event_bus, settings):
    # a short freshness window -> the EMA evidence decays out well before we
    # check the snapshot, while RSI (published just before the check) is
    # still comfortably inside the window.
    aggregator = EvidenceAggregator(settings, freshness_window_seconds=0.2)
    aggregator.attach(event_bus)

    await _publish_evidence(event_bus, "EMA", "Bullish EMA Cross", "bullish")
    await asyncio.sleep(0.25)  # let it decay past the window

    await _publish_evidence(event_bus, "RSI", "RSI Oversold", "bullish")
    await asyncio.sleep(0.02)

    snap = aggregator.snapshot("NVDA")
    # the EMA evidence has decayed out; only the fresh RSI one remains
    assert len(snap.active_evidence) == 1
    assert snap.active_evidence[0].source == "RSI"

    # but it's still in the preserved history
    sources_in_history = [e.source for e in aggregator.history("NVDA")]
    assert "EMA" in sources_in_history
    assert "RSI" in sources_in_history


async def test_history_bounded_by_max_history_per_symbol(event_bus, settings):
    aggregator = EvidenceAggregator(settings, max_history_per_symbol=5)
    aggregator.attach(event_bus)

    for i in range(10):
        await _publish_evidence(event_bus, "EMA", f"Signal {i}", "bullish")
    await asyncio.sleep(0.1)

    assert len(aggregator.history("NVDA")) == 5
    # the oldest ones were trimmed, newest retained
    titles = [e.title for e in aggregator.history("NVDA")]
    assert titles == [f"Signal {i}" for i in range(5, 10)]


async def test_symbols_are_isolated_from_each_other(event_bus, settings):
    aggregator = EvidenceAggregator(settings)
    aggregator.attach(event_bus)

    await _publish_evidence(event_bus, "EMA", "Bullish EMA Cross", "bullish", symbol="AAPL")
    await _publish_evidence(event_bus, "EMA", "Bearish EMA Cross", "bearish", symbol="TSLA")
    await asyncio.sleep(0.05)

    assert len(aggregator.snapshot("AAPL").active_evidence) == 1
    assert len(aggregator.snapshot("TSLA").active_evidence) == 1
    assert aggregator.snapshot("AAPL").active_evidence[0].direction == "bullish"
    assert aggregator.snapshot("TSLA").active_evidence[0].direction == "bearish"


async def test_snapshot_for_unknown_symbol_is_empty(settings):
    aggregator = EvidenceAggregator(settings)
    snap = aggregator.snapshot("NOTHING_EVER_PUBLISHED")
    assert snap.active_evidence == []
    assert snap.has_conflict is False
