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

from app.discord.command_plugin import (
    CommandOption,
    DiscordCommandPlugin,
    is_valid_command_name,
    is_valid_option_name,
)
from app.discord.dispatch import CommandButton, CommandContext, CommandResponse, dispatch_command
from app.event_bus.bus import EventBus
from app.logging import get_logger
from app.plugins.registry import PluginRegistry

log = get_logger(__name__)

_BUTTON_STYLES: dict[str, discord.ButtonStyle] = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


def _build_parameterized_callback(options: tuple[CommandOption, ...], invoke: Any) -> Any:
    """Dynamically build a real function whose signature matches ``options``.

    discord.py derives a slash command's parameters (name, type, required)
    by inspecting the callback's Python signature and type hints — there is
    no supported way to attach options to an ``app_commands.Command``
    without a matching callback signature. Since command plugins declare
    their options as data (``DiscordCommandPlugin.parameters``), not as a
    hand-written function, this builds that function at registration time
    instead of asking every command plugin to hand-write one. Every
    declared option is currently typed ``str`` (see ``CommandOption``).

    ``invoke`` is called as ``await invoke(interaction, {name: value, ...})``
    once discord.py has parsed the real interaction.
    """
    pieces = [f"{opt.name}: str" if opt.required else f"{opt.name}: str = None" for opt in options]
    signature = ", ".join(pieces)
    kwargs_literal = ", ".join(f'"{opt.name}": {opt.name}' for opt in options)
    source = (
        f"async def _generated_callback(interaction, {signature}):\n"
        f"    await __invoke__(interaction, {{{kwargs_literal}}})\n"
    )
    namespace: dict[str, Any] = {"__invoke__": invoke}
    # The only way to give discord.py a real parameter signature built from
    # plugin-declared data instead of a hand-written function — see the
    # docstring above.
    exec(source, namespace)  # noqa: S102
    generated = namespace["_generated_callback"]
    app_commands.describe(**{opt.name: opt.description for opt in options})(generated)
    return generated


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

            bad_option = next((opt for opt in plugin.parameters if not is_valid_option_name(opt.name)), None)
            if bad_option is not None:
                log.warning("invalid_command_option_name_skipped", plugin=name, option=bad_option.name)
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
        async def _invoke(interaction: discord.Interaction, values: dict[str, Any]) -> None:
            ctx = CommandContext(
                user_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                args=values,
            )
            response = await dispatch_command(plugin, self._event_bus, ctx)
            await self._send_response(interaction, response)

        if not plugin.parameters:

            async def _callback(interaction: discord.Interaction) -> None:
                await _invoke(interaction, {})

            return _callback

        return _build_parameterized_callback(plugin.parameters, _invoke)

    async def _send_response(self, interaction: discord.Interaction, response: CommandResponse) -> None:
        kwargs: dict[str, Any] = {"ephemeral": response.ephemeral}
        if response.buttons:
            kwargs["view"] = self._build_view(response.buttons)
        await interaction.response.send_message(response.content, **kwargs)

    def _build_view(self, buttons: list[CommandButton]) -> "discord.ui.View":
        view = discord.ui.View(timeout=None)
        for spec in buttons:
            view.add_item(self._build_button(spec))
        return view

    def _build_button(self, spec: CommandButton) -> "discord.ui.Button":
        button = discord.ui.Button(
            label=spec.label,
            style=_BUTTON_STYLES.get(spec.style, discord.ButtonStyle.secondary),
            custom_id=spec.custom_id,
            disabled=spec.disabled,
        )

        async def _on_click(interaction: discord.Interaction) -> None:
            # custom_id convention: "{command}:{action}:{extra}" — see
            # CommandButton's docstring. "dismiss" is the only action with
            # real behavior today; every other action names a system
            # (Chart/News/History/Backtest/Journal/Watch) that doesn't
            # exist yet, so it gets an honest placeholder instead of
            # silently doing nothing or pretending to work.
            parts = spec.custom_id.split(":")
            action = parts[1] if len(parts) > 1 else parts[0]
            try:
                if action == "dismiss":
                    if interaction.message is not None:
                        await interaction.message.delete()
                    else:
                        await interaction.response.send_message("Dismissed.", ephemeral=True)
                    return
                await interaction.response.send_message(
                    f"'{spec.label}' isn't built yet — see docs/MILESTONES.md for the roadmap.",
                    ephemeral=True,
                )
            except Exception:
                log.exception("button_interaction_failed", custom_id=spec.custom_id)

        button.callback = _on_click
        return button

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
