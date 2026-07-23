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


async def test_bot_registers_help_ping_analyze_and_scan_commands(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)

    bot = TradingBot(settings, event_bus, registry)
    registered = bot.register_command_plugins()

    assert sorted(registered) == ["analyze", "ping", "scan"]

    tree_commands = sorted(c.name for c in bot.tree.get_commands())
    assert tree_commands == ["analyze", "help", "ping", "scan"]

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


def _stub_param_plugin_class(parameters, command_name="stubparam"):
    """Builds a throwaway DiscordCommandPlugin subclass declaring the given
    CommandOption tuple — used to test option registration/callback wiring
    without depending on the real /analyze plugin's evidence/reasoning
    dependencies."""
    from app.discord.command_plugin import DiscordCommandPlugin
    from app.discord.dispatch import CommandContext, CommandResponse
    from app.plugins.base import PluginHealth, PluginPermission

    # Captured under a different name than the class attribute it seeds --
    # assigning `command_name = command_name` inside the class body would
    # make Python treat `command_name` as local to the class body for the
    # *entire* block (even the earlier f-string reference), raising a
    # NameError instead of reading the enclosing function's parameter.
    _name = command_name

    class _StubParamCommand(DiscordCommandPlugin):
        name = f"Stub-{_name}"
        command_name = _name
        command_description = "stub with parameters"

        def __init__(self, context):
            super().__init__(context)
            self.calls: list[CommandContext] = []

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
            self.calls.append(ctx)
            return CommandResponse(content=f"args={ctx.args}")

    _StubParamCommand.parameters = parameters
    return _StubParamCommand


async def test_registered_command_derives_real_string_option(event_bus, settings):
    from app.discord.command_plugin import CommandOption
    from app.plugins.base import PluginContext

    plugin_class = _stub_param_plugin_class(
        (CommandOption(name="symbol", description="Ticker symbol", required=True),)
    )
    registry = PluginRegistry(event_bus, settings)
    plugin = plugin_class(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()
    registry._plugins[plugin.name] = plugin  # test-only direct injection

    bot = TradingBot(settings, event_bus, registry)
    bot.register_command_plugins()

    command = bot.tree.get_command("stubparam")
    assert command is not None
    assert len(command.parameters) == 1
    option = command.parameters[0]
    assert option.name == "symbol"
    assert option.required is True
    assert option.description == "Ticker symbol"

    await registry.shutdown_all()


async def test_parameterized_callback_populates_ctx_args(event_bus, settings):
    from app.discord.command_plugin import CommandOption
    from app.plugins.base import PluginContext

    plugin_class = _stub_param_plugin_class(
        (CommandOption(name="symbol", description="Ticker symbol", required=True),)
    )
    registry = PluginRegistry(event_bus, settings)
    plugin = plugin_class(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()
    registry._plugins[plugin.name] = plugin

    bot = TradingBot(settings, event_bus, registry)
    bot.register_command_plugins()

    command = bot.tree.get_command("stubparam")
    interaction = _fake_interaction()
    await command.callback(interaction, symbol="NVDA")

    assert len(plugin.calls) == 1
    assert plugin.calls[0].args == {"symbol": "NVDA"}
    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    content = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content")
    assert content == "args={'symbol': 'NVDA'}"

    await registry.shutdown_all()


async def test_invalid_option_name_is_skipped_not_fatal(event_bus, settings):
    from app.discord.command_plugin import CommandOption
    from app.plugins.base import PluginContext

    plugin_class = _stub_param_plugin_class(
        (CommandOption(name="Has Spaces", description="bad name", required=True),),
        command_name="badoption",
    )
    registry = PluginRegistry(event_bus, settings)
    plugin = plugin_class(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()
    registry._plugins[plugin.name] = plugin

    bot = TradingBot(settings, event_bus, registry)
    registered = bot.register_command_plugins()  # must not raise

    assert "badoption" not in registered
    assert bot.tree.get_command("badoption") is None

    await registry.shutdown_all()


async def test_response_with_buttons_attaches_a_view(event_bus, settings):
    from app.discord.dispatch import CommandButton

    registry = PluginRegistry(event_bus, settings)
    await registry.load_all(PROJECT_ROOT)
    bot = TradingBot(settings, event_bus, registry)

    interaction = _fake_interaction()
    from app.discord.dispatch import CommandResponse

    response = CommandResponse(
        content="hello",
        buttons=[CommandButton(label="Dismiss", custom_id="dismiss:NVDA", style="danger")],
    )
    await bot._send_response(interaction, response)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    assert "view" in call_kwargs.kwargs
    view = call_kwargs.kwargs["view"]
    assert len(view.children) == 1
    assert view.children[0].label == "Dismiss"

    await registry.shutdown_all()


async def test_dismiss_button_deletes_the_message(event_bus, settings):
    from app.discord.dispatch import CommandButton

    registry = PluginRegistry(event_bus, settings)
    bot = TradingBot(settings, event_bus, registry)

    button = bot._build_button(CommandButton(label="Dismiss", custom_id="dismiss:NVDA", style="danger"))

    interaction = _fake_interaction()
    interaction.message.delete = AsyncMock()
    await button.callback(interaction)

    interaction.message.delete.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


async def test_placeholder_button_sends_not_built_yet_reply(event_bus, settings):
    from app.discord.dispatch import CommandButton

    registry = PluginRegistry(event_bus, settings)
    bot = TradingBot(settings, event_bus, registry)

    button = bot._build_button(CommandButton(label="Chart", custom_id="chart:NVDA"))

    interaction = _fake_interaction()
    await button.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    content = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content")
    assert "Chart" in content
    assert "isn't built yet" in content
    assert call_kwargs.kwargs.get("ephemeral") is True


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
