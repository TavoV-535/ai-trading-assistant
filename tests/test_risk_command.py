"""
Tests for the /risk command plugin (plugins/commands/risk/plugin.py).

Same "load the plugin module by path, exercise execute() against a real
CapitalProtectionEngine on a real event bus" pattern
tests/test_watchlist_command.py uses.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.capital_protection.engine import CapitalProtectionEngine
from app.discord.dispatch import CommandContext
from app.event_bus.events import ACTION_DIRECTIONS, DecisionRecorded
from app.plugins.base import PluginContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_risk_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "commands" / "risk" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_risk_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RiskPlugin


RiskPlugin = _load_risk_plugin_class()


async def test_risk_gracefully_degrades_without_capital_protection_engine(event_bus, settings):
    plugin = RiskPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))

    assert "isn't available" in response.content
    assert response.ephemeral is True


async def test_risk_read_mode_shows_status_and_available_profiles(event_bus, settings):
    engine = CapitalProtectionEngine(settings)
    engine.attach(event_bus)

    # A bullish decision drives the engine's continuously-evolving state
    # forward (synthesizes a TradeOpened/TradeClosed pair, publishes a full
    # round of RiskEvents) so /risk has something real to render.
    bullish_action = next(action for action, direction in ACTION_DIRECTIONS.items() if direction == "bullish")
    await event_bus.publish(
        DecisionRecorded(
            source="test",
            symbol="NVDA",
            confidence=80.0,
            simulated_action=bullish_action,
            price_at_decision=100.0,
            bar_index=0,
            lookahead_bars=5,
            outcome="correct",
            outcome_price_change_pct=1.0,
            outcome_pending=False,
        )
    )
    await asyncio.sleep(0.05)

    plugin = RiskPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, capital_protection_engine=engine)
    )
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))

    assert "**Capital protection status**" in response.content
    assert settings.capital_protection.active_profile in response.content
    assert "Equity:" in response.content
    assert "Available profiles:" in response.content
    for name in settings.capital_protection.profiles:
        assert name in response.content
    assert [b.label for b in response.buttons] == ["Refresh", "Dismiss"]


async def test_risk_profile_switch_succeeds_for_a_known_profile(event_bus, settings):
    engine = CapitalProtectionEngine(settings)
    engine.attach(event_bus)

    plugin = RiskPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, capital_protection_engine=engine)
    )
    await plugin.initialize()

    response = await plugin.execute(
        CommandContext(user_id="1", guild_id=None, channel_id=None, args={"profile": "prop_firm"})
    )

    assert "switched to 'prop_firm'" in response.content
    assert response.ephemeral is True
    assert engine.status().active_profile == "prop_firm"


async def test_risk_profile_switch_reports_unknown_profile_names(event_bus, settings):
    engine = CapitalProtectionEngine(settings)
    engine.attach(event_bus)

    plugin = RiskPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, capital_protection_engine=engine)
    )
    await plugin.initialize()

    response = await plugin.execute(
        CommandContext(user_id="1", guild_id=None, channel_id=None, args={"profile": "does_not_exist"})
    )

    assert "Unknown Risk Profile 'does_not_exist'" in response.content
    assert "Available profiles:" in response.content
    assert response.ephemeral is True
    # The active profile is untouched by a failed switch.
    assert engine.status().active_profile == settings.capital_protection.active_profile
