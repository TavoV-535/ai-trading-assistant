"""
Tests the parts of TradingBot that don't require a live gateway connection:
building the command tree and registering plugins. Actually connecting to
Discord (``bot.start(token)``) can only be verified in a real environment
with network access and a real bot token — see docs/DISCORD_BOT_SETUP.md.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.discord.bot import TradingBot
from app.plugins import PluginRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_interaction(user_id: int = 111, guild_id: int | None = 222, channel_id: int | None = 333):
    """A minimal duck-typed stand-in for discord.Interaction — only exposes
    the attributes TradingBot's callbacks actually touch, so this works
    without a live gateway connection."""
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.guild_id = guild_id
    interaction.channel_id = channel_id
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_bot_registers_help_and_ping_commands(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    bot = TradingBot(settings, event_bus, registry)
    registered = bot.register_command_plugins()

    assert registered == ["ping"]

    tree_commands = sorted(c.name for c in bot.tree.get_commands())
    assert tree_commands == ["help", "ping"]

    await registry.shutdown_all()


async def test_bot_skips_non_command_plugins(event_bus, settings):
    """EMA is a PluginBase but not a DiscordCommandPlugin — it must never
    become a slash command."""
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    bot = TradingBot(settings, event_bus, registry)
    bot.register_command_plugins()

    tree_commands = {c.name for c in bot.tree.get_commands()}
    assert "EMA" not in tree_commands
    assert "ema" not in tree_commands

    await registry.shutdown_all()


async def test_bot_registration_is_idempotent_safe_against_duplicate_names(event_bus, settings):
    """A second plugin claiming the same command_name is skipped, not a crash."""
    from app.discord.command_plugin import DiscordCommandPlugin
    from app.discord.dispatch import CommandContext, CommandResponse
    from app.plugins.base import PluginContext, PluginHealth, PluginPermission

    class _DuplicatePing(DiscordCommandPlugin):
        name = "DuplicatePing"
        command_name = "ping"  # collides with the real Ping plugin
        command_description = "duplicate"

        async def initialize(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

        async def health(self) -> PluginHealth:
            return PluginHealth(status="healthy")

        def config(self) -> dict:
            return {}

        def permissions(self) -> list:
            return [PluginPermission.EVENTS_PUBLISH]

        async def execute(self, ctx: CommandContext) -> CommandResponse:
            return CommandResponse(content="duplicate")

    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    dup = _DuplicatePing(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await dup.initialize()
    registry._plugins["DuplicatePing"] = dup  # test-only injection to force the collision

    bot = TradingBot(settings, event_bus, registry)
    registered = bot.register_command_plugins()  # must not raise

    assert registered.count("ping") == 1

    await registry.shutdown_all()


async def test_ping_callback_replies_via_interaction(event_bus, settings):
    """Exercises the actual bridge between a discord.Interaction and
    dispatch_command — everything except the live gateway connection
    itself."""
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    bot = TradingBot(settings, event_bus, registry)
    bot.register_command_plugins()

    ping_command = bot.tree.get_command("ping")
    assert ping_command is not None

    interaction = _fake_interaction(user_id=999)
    await ping_command.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    content = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content")
    assert content.startswith("Pong!")
    assert call_kwargs.kwargs.get("ephemeral") is True

    await registry.shutdown_all()


async def test_help_callback_lists_registered_commands(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    bot = TradingBot(settings, event_bus, registry)
    bot.register_command_plugins()

    help_command = bot.tree.get_command("help")
    interaction = _fake_interaction()
    await help_command.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    content = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content")
    assert "/help" in content
    assert "/ping" in content
    assert call_kwargs.kwargs.get("ephemeral") is True

    await registry.shutdown_all()
