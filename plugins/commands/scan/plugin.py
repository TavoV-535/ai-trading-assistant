"""
/scan — status report for the Scanner Engine.

Zero parameters. Lists every loaded scanner plugin (watchlist, timeframes,
interval, health) and which market data provider(s) are currently
configured. Uses the Discord Action Registry for its buttons (Refresh /
Dismiss) — the same registry ``/analyze`` uses — which is the point:
adding this second command required zero new Discord-specific button code,
only declaring which actions it wants (see ``_ACTIONS`` below).

Reads ``context.plugin_registry`` and ``context.market_data_service``
directly — the same documented, narrow, read-only ``PluginContext``
exception ``/analyze`` uses for ``evidence_aggregator``/
``reasoning_engine`` (see ``app/plugins/base.py``'s docstring). Never
mutates either.
"""
from __future__ import annotations

from typing import Any

from app.discord.actions import ACTION_REGISTRY
from app.discord.command_plugin import DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission
from app.scanner.plugin import ScannerPlugin

log = get_logger(__name__)

_ACTIONS = ["refresh", "dismiss"]


class ScanStatusPlugin(DiscordCommandPlugin):
    """Reports what the Scanner Engine is currently watching."""

    name = "ScanStatus"
    version = "0.1.0"
    category = "commands"
    command_name = "scan"
    command_description = "Show what the Scanner Engine is currently watching."

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._invocations = 0

    async def initialize(self) -> None:
        log.info("scan_status_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("scan_status_plugin_shutdown", invocations=self._invocations)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._invocations} invocation(s)")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        self._invocations += 1

        registry = self.context.plugin_registry
        if registry is None:
            return CommandResponse(
                content="Scanner status isn't available right now — the plugin registry isn't wired up.",
                ephemeral=True,
            )

        scanners = [p for p in registry.plugins.values() if isinstance(p, ScannerPlugin)]
        content = _format_status(scanners_health=[(s, await s.health()) for s in sorted(scanners, key=lambda s: s.name)])

        market_data = self.context.market_data_service
        if market_data is not None:
            providers = [p.provider_name for p in market_data.providers]
            content += f"\n\nMarket data provider(s): {', '.join(providers) if providers else 'none configured'}"

        return CommandResponse(content=content, buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target="scan"))


def _format_status(scanners_health: list[tuple[ScannerPlugin, PluginHealth]]) -> str:
    if not scanners_health:
        return "**Scanner Engine status**\n\nNo scanners are currently loaded."

    lines = ["**Scanner Engine status**", ""]
    for scanner, health in scanners_health:
        watchlist = ", ".join(scanner.watchlist) or "nothing"
        timeframes = ", ".join(scanner.timeframes) or "none"
        lines.append(
            f"**{scanner.name}** _({health.status})_ — watching {watchlist} "
            f"@ {timeframes}, every {scanner.interval_seconds:.0f}s"
        )
        if health.detail:
            lines.append(f"  {health.detail}")
    return "\n".join(lines)
