"""
Tests for the Donchian plugin's configurable repeat_policy and sequence
metadata (Milestone 4 requirement: repeated breakout behavior must be
configurable, never hard-coded suppression, and every occurrence — whether
published or not — must be tagged with sequence metadata so a strategy can
reinterpret it later).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.event_bus import EvidenceProduced, MarketDataUpdated
from app.plugins import PluginRegistry
from app.plugins.base import PluginContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _uptrend_bars(n: int, base: float = 100.0):
    """Continuous ramp where each bar's close IS its high (no upper wick),
    so the close itself sets a fresh channel high every single bar — a
    single, unbroken breakout streak. (A wick above the close would let
    the prior bar's high outrun the next bar's close and stall the streak
    after just one breakout.)"""
    return [{"open": base + i - 1, "high": base + i, "low": base + i - 5, "close": base + i} for i in range(n)]


async def _publish_bars(event_bus, symbol, bars, timeframe="1m"):
    for bar in bars:
        await event_bus.publish(
            MarketDataUpdated(
                symbol=symbol,
                price=bar["close"],
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                timeframe=timeframe,
            )
        )
    await asyncio.sleep(0.1)


async def _make_donchian(event_bus, settings, *, repeat_policy: str = "every_breakout", period: int = 5):
    """Constructs a standalone DonchianPlugin (bypassing the registry) so
    each test can control repeat_policy directly without a config.yaml."""
    import importlib.util

    plugin_path = PROJECT_ROOT / "plugins" / "indicators" / "donchian" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_donchian_test_module", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    context = PluginContext(event_bus=event_bus, settings=settings, plugin_config={"period": period, "repeat_policy": repeat_policy})
    plugin = module.DonchianPlugin(context)
    await plugin.initialize()
    return plugin


async def test_every_breakout_fires_on_every_new_high(event_bus, settings):
    await _make_donchian(event_bus, settings, repeat_policy="every_breakout")

    evidence = []

    async def on_evidence(e: EvidenceProduced) -> None:
        evidence.append(e)

    event_bus.subscribe(EvidenceProduced, on_evidence)

    # warmup (channel needs `period` bars) then a sustained ramp
    flat = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(6)]
    ramp = _uptrend_bars(15, base=100)
    await _publish_bars(event_bus, "TEST1", flat + ramp)

    assert len(evidence) > 5  # fires repeatedly through the sustained ramp


async def test_first_breakout_fires_only_once_per_streak(event_bus, settings):
    await _make_donchian(event_bus, settings, repeat_policy="first_breakout")

    evidence = []

    async def on_evidence(e: EvidenceProduced) -> None:
        evidence.append(e)

    event_bus.subscribe(EvidenceProduced, on_evidence)

    flat = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(6)]
    ramp = _uptrend_bars(15, base=100)
    await _publish_bars(event_bus, "TEST2", flat + ramp)

    assert len(evidence) == 1
    assert evidence[0].evidence.metadata["is_first_in_sequence"] is True


async def test_after_pullback_suppresses_the_cold_start_breakout(event_bus, settings):
    """The very first breakout a symbol ever sees has no real pullback
    behind it (there's no prior streak to have pulled back from) — a
    strategy asking for `after_pullback` shouldn't get evidence for that
    cold-start case, only for a genuine second wave following a real
    pullback."""
    await _make_donchian(event_bus, settings, repeat_policy="after_pullback")

    evidence = []

    async def on_evidence(e: EvidenceProduced) -> None:
        evidence.append(e)

    event_bus.subscribe(EvidenceProduced, on_evidence)

    flat = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(6)]
    first_wave = _uptrend_bars(10, base=100)  # highs climb to 109
    pullback = [{"open": 104, "high": 105, "low": 100, "close": 102} for _ in range(3)]  # stays below 109 -> no new highs, resets the streak
    second_wave = _uptrend_bars(10, base=110)  # starts above 109 -> a fresh breakout

    await _publish_bars(event_bus, "TEST3", flat + first_wave + pullback + second_wave)

    # cold-start first wave produced no evidence (is_first_ever==True, excluded)
    # only the first breakout of the second wave (after a real pullback) fired
    assert len(evidence) == 1
    meta = evidence[0].evidence.metadata
    assert meta["is_first_in_sequence"] is True
    assert meta["is_first_ever"] is False


async def test_sequence_metadata_always_present_regardless_of_policy(event_bus, settings):
    """even under every_breakout (which publishes everything), the sequence
    metadata must still be there so a downstream strategy can filter later."""
    await _make_donchian(event_bus, settings, repeat_policy="every_breakout")

    evidence = []

    async def on_evidence(e: EvidenceProduced) -> None:
        evidence.append(e)

    event_bus.subscribe(EvidenceProduced, on_evidence)

    flat = [{"open": 100, "high": 101, "low": 99, "close": 100} for _ in range(6)]
    ramp = _uptrend_bars(10, base=100)
    await _publish_bars(event_bus, "TEST4", flat + ramp)

    assert len(evidence) > 1
    sequence_numbers = [e.evidence.metadata["breakout_sequence"] for e in evidence]
    assert sequence_numbers == sorted(sequence_numbers)  # strictly increasing through the streak
    assert sequence_numbers[0] == 1
    assert all("bars_since_first_breakout" in e.evidence.metadata for e in evidence)
    assert all("distance_from_channel" in e.evidence.metadata for e in evidence)
    # only the very first occurrence in the whole run is "first ever"
    assert evidence[0].evidence.metadata["is_first_ever"] is True
    assert all(e.evidence.metadata["is_first_ever"] is False for e in evidence[1:])


async def test_invalid_repeat_policy_falls_back_to_every_breakout(event_bus, settings):
    plugin = await _make_donchian(event_bus, settings, repeat_policy="not_a_real_policy")
    assert plugin.config()["repeat_policy"] == "every_breakout"


async def test_donchian_still_discoverable_via_registry_with_default_config(event_bus, settings):
    """Sanity check that the real config.yaml (repeat_policy: every_breakout)
    still loads cleanly through the normal plugin discovery path, not just
    the test's manual construction path."""
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    assert "Donchian" in registry.plugins
    assert registry.plugins["Donchian"].config()["repeat_policy"] == "every_breakout"
    await registry.shutdown_all()
