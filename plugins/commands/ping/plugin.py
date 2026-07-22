"""
/ping — reference command plugin.

Proves the full Discord command pipeline end to end, the same way the EMA
plugin proved the indicator pipeline in Milestone 1: discovered by the
same loader, initialized by the same registry, invoked through the same
:func:`~app.discord.dispatch.dispatch_command`, and its invocation shows up
in ``event_log`` automatically via ``CommandInvoked``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.discord.command_plugin import DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission

log = get_logger(__name__)


class PingPlugin(DiscordCommandPlugin):
    """Replies with Pong and how long the bot has been connected."""

    name = "Ping"
    version = "0.1.0"
    category = "commands"
    command_name = "ping"
    command_description = "Check that the bot is alive and connected."

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._started_at: datetime | None = None
        self._invocations = 0

    async def initialize(self) -> None:
        self._started_at = datetime.now(timezone.utc)
        log.info("ping_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("ping_plugin_shutdown", invocations=self._invocations)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._invocations} invocation(s)")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        self._invocations += 1
        uptime = "unknown"
        if self._started_at is not None:
            seconds = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
            uptime = f"{seconds}s"
        return CommandResponse(content=f"Pong! Uptime: {uptime}.", ephemeral=True)
