"""
Tests for the reference Earnings intelligence plugin
(plugins/intelligence/earnings/plugin.py).
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.event_bus.events import EarningsReleased, EvidenceProduced
from app.plugins.base import PluginContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_earnings_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "intelligence" / "earnings" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_earnings_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EarningsPlugin


EarningsPlugin = _load_earnings_plugin_class()


async def test_earnings_plugin_publishes_release_and_evidence_pairs(event_bus, settings):
    plugin = EarningsPlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={"watchlist": ["NVDA", "AAPL", "TSLA"], "interval_seconds": 10},
        )
    )

    releases: list[EarningsReleased] = []
    evidence_events: list[EvidenceProduced] = []
    event_bus.subscribe(EarningsReleased, lambda e: releases.append(e))
    event_bus.subscribe(EvidenceProduced, lambda e: evidence_events.append(e))

    for _ in range(60):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    assert len(releases) > 0
    assert len(releases) == len(evidence_events)
    for release, ev in zip(releases, evidence_events):
        assert ev.evidence.symbol == release.symbol
        assert ev.evidence.category == "Earnings"
        assert ev.evidence.metadata["surprise_percent"] == release.surprise_percent
        # eps_actual is derived from estimate + surprise, consistently
        expected_actual = round(release.eps_estimate * (1 + release.surprise_percent / 100), 2)
        assert release.eps_actual == expected_actual
        if release.surprise_percent >= 5:
            assert ev.evidence.direction == "bullish"
        elif release.surprise_percent <= -5:
            assert ev.evidence.direction == "bearish"
        else:
            assert ev.evidence.direction == "neutral"


async def test_earnings_plugin_tags_earnings_season_when_multiple_symbols_release_together(event_bus, settings):
    plugin = EarningsPlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={"watchlist": ["NVDA", "AAPL", "TSLA"], "interval_seconds": 10},
        )
    )

    evidence_events: list[EvidenceProduced] = []
    event_bus.subscribe(EvidenceProduced, lambda e: evidence_events.append(e))

    for _ in range(200):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    hinted = [e for e in evidence_events if e.evidence.metadata.get("context_hint") == "earnings_season"]
    assert hinted, "expected at least one earnings_season-tagged release across 200 polls of a 3-symbol watchlist"


async def test_earnings_plugin_single_symbol_watchlist_never_tags_earnings_season(event_bus, settings):
    plugin = EarningsPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={"watchlist": ["NVDA"], "interval_seconds": 10})
    )

    evidence_events: list[EvidenceProduced] = []
    event_bus.subscribe(EvidenceProduced, lambda e: evidence_events.append(e))

    for _ in range(60):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    assert evidence_events  # sanity
    assert all("context_hint" not in e.evidence.metadata for e in evidence_events)
