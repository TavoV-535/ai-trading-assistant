from __future__ import annotations

from pathlib import Path

from app.discord import CommandContext, DiscordCommandPlugin, dispatch_command
from app.plugins import PluginRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def test_ping_discovered_as_command_plugin(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    assert "Ping" in registry.plugins
    ping = registry.get("Ping")
    assert isinstance(ping, DiscordCommandPlugin)
    assert ping.command_name == "ping"
    assert ping.category == "commands"

    await registry.shutdown_all()


async def test_ping_execute_via_dispatch(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    ping = registry.get("Ping")

    ctx = CommandContext(user_id="1", guild_id="2", channel_id="3", args={})
    response = await dispatch_command(ping, event_bus, ctx)

    assert response.content.startswith("Pong!")
    assert response.ephemeral is True

    health = await ping.health()
    assert health.status == "healthy"
    assert "1 invocation" in health.detail

    await registry.shutdown_all()
