"""
The Discord bot — the "Command Engine" in the architecture diagram.

Thin by design: this module's only job is translating between discord.py's
``Interaction`` objects and the framework-agnostic
:func:`~app.discord.dispatch.dispatch_command`. All the logic worth unit
testing lives in ``dispatch.py``; this file is the part that genuinely
needs a live Discord connection to fully exercise, which is why it's kept
as small as possible.

Every ``/command`` other than the built-in ``/help`` comes from a
:class:`~app.discord.command_plugin.DiscordCommandPlugin` discovered by the
same :class:`~app.plugins.registry.PluginRegistry` used for every other
plugin category — dropping a folder under ``plugins/commands/`` is the
entire integration step.
"""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from app.discord.command_plugin import DiscordCommandPlugin, is_valid_command_name
from app.discord.dispatch import CommandContext, dispatch_command
from app.event_bus.bus import EventBus
from app.logging import get_logger
from app.plugins.registry import PluginRegistry

log = get_logger(__name__)


class TradingBot(discord.Client):
    def __init__(self, settings: Any, event_bus: EventBus, plugin_registry: PluginRegistry) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._settings = settings
        self._event_bus = event_bus
        self._plugin_registry = plugin_registry
        self._registered_commands: dict[str, DiscordCommandPlugin] = {}
        self._register_help_command()

    # ---------------------------------------------------------------- setup

    def _register_help_command(self) -> None:
        @self.tree.command(name="help", description="List every available command")
        async def _help(interaction: discord.Interaction) -> None:
            lines = ["**/help** — list every available command"]
            for name, plugin in sorted(self._registered_commands.items()):
                lines.append(f"**/{name}** — {plugin.command_description or 'No description'}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

    def register_command_plugins(self) -> list[str]:
        """Register every loaded :class:`DiscordCommandPlugin` as a slash command.

        Called once during ``setup_hook``, after the plugin registry has
        already loaded everything — an invalid name or a name collision is
        logged and skipped, the same isolation policy as plugin loading
        itself.
        """
        registered: list[str] = []
        for name, plugin in self._plugin_registry.plugins.items():
            if not isinstance(plugin, DiscordCommandPlugin):
                continue
            if not is_valid_command_name(plugin.command_name):
                log.warning("invalid_command_name_skipped", plugin=name, command_name=plugin.command_name)
                continue
            if plugin.command_name in self._registered_commands:
                log.warning("command_name_collision_skipped", command_name=plugin.command_name)
                continue

            self.tree.command(
                name=plugin.command_name,
                description=plugin.command_description or "No description",
            )(self._make_callback(plugin))
            self._registered_commands[plugin.command_name] = plugin
            registered.append(plugin.command_name)

        log.info("discord_commands_registered", commands=registered)
        return registered

    def _make_callback(self, plugin: DiscordCommandPlugin):
        async def _callback(interaction: discord.Interaction) -> None:
            ctx = CommandContext(
                user_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                args={},
            )
            response = await dispatch_command(plugin, self._event_bus, ctx)
            await interaction.response.send_message(response.content, ephemeral=response.ephemeral)

        return _callback

    async def setup_hook(self) -> None:
        """Called by discord.py once, before it opens the gateway connection."""
        self.register_command_plugins()

        guild_id = self._settings.discord_guild_id
        if guild_id:
            guild_obj = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.info("discord_commands_synced", scope="guild", guild_id=guild_id)
        else:
            await self.tree.sync()
            log.info("discord_commands_synced", scope="global")

    async def on_ready(self) -> None:
        log.info("discord_bot_ready", user=str(self.user))
