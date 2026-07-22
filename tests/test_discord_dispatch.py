from __future__ import annotations

import asyncio

from app.discord.command_plugin import DiscordCommandPlugin, is_valid_command_name
from app.discord.dispatch import CommandContext, CommandResponse, dispatch_command
from app.event_bus import CommandFailed, CommandInvoked
from app.plugins.base import PluginHealth, PluginPermission


class _StubCommand(DiscordCommandPlugin):
    name = "Stub"
    command_name = "stub"
    command_description = "A stub command for testing."

    def __init__(self, context):
        super().__init__(context)
        self.should_fail = False
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
        if self.should_fail:
            raise RuntimeError("simulated command failure")
        return CommandResponse(content=f"handled for user {ctx.user_id}")


def _make_stub(event_bus, plugin_config: dict | None = None) -> _StubCommand:
    from app.plugins.base import PluginContext

    return _StubCommand(PluginContext(event_bus=event_bus, settings=None, plugin_config=plugin_config or {}))


async def test_dispatch_command_returns_plugin_response(event_bus):
    plugin = _make_stub(event_bus)
    ctx = CommandContext(user_id="42", guild_id="1", channel_id="2", args={})

    response = await dispatch_command(plugin, event_bus, ctx)

    assert response.content == "handled for user 42"
    assert plugin.calls == [ctx]
    await event_bus.shutdown()


async def test_dispatch_command_publishes_command_invoked(event_bus):
    plugin = _make_stub(event_bus)
    ctx = CommandContext(user_id="42", guild_id="1", channel_id="2", args={"symbol": "NVDA"})

    seen = []

    async def on_invoked(event: CommandInvoked) -> None:
        seen.append(event)

    event_bus.subscribe(CommandInvoked, on_invoked)
    await dispatch_command(plugin, event_bus, ctx)
    await asyncio.sleep(0.05)

    assert len(seen) == 1
    assert seen[0].command == "stub"
    assert seen[0].user_id == "42"
    assert seen[0].args == {"symbol": "NVDA"}
    await event_bus.shutdown()


async def test_dispatch_command_isolates_plugin_exception(event_bus):
    plugin = _make_stub(event_bus)
    plugin.should_fail = True
    ctx = CommandContext(user_id="42", guild_id=None, channel_id=None, args={})

    failed_events = []

    async def on_failed(event: CommandFailed) -> None:
        failed_events.append(event)

    event_bus.subscribe(CommandFailed, on_failed)

    response = await dispatch_command(plugin, event_bus, ctx)  # must not raise
    await asyncio.sleep(0.05)

    assert response.ephemeral is True
    assert "went wrong" in response.content
    assert len(failed_events) == 1
    assert failed_events[0].error == "simulated command failure"
    await event_bus.shutdown()


def test_is_valid_command_name():
    assert is_valid_command_name("ping") is True
    assert is_valid_command_name("analyze-nvda") is True
    assert is_valid_command_name("has_underscore") is True
    assert is_valid_command_name("") is False
    assert is_valid_command_name("Has Spaces") is False
    assert is_valid_command_name("UPPERCASE") is False
    assert is_valid_command_name("a" * 33) is False
