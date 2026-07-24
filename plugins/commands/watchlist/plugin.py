"""
/watchlist — the Portfolio Intelligence Layer's ranked output.

Zero parameters. Lists every symbol on ``settings.portfolio.watchlist``,
highest ``priority_score`` first, with the transparent breakdown behind
each score (evidence strength, external-intelligence freshness, market
context, confidence trend, strategy matches, alert-suppression dampening)
— the same explainability convention ``/analyze`` already uses for the
Confidence Weighting Framework.

This is the proactive counterpart to ``/analyze``: ``/analyze NVDA`` asks
"what does the system currently think about NVDA," ``/watchlist`` asks
"which of my configured symbols most deserves my attention right now" —
continuous monitoring surfaced on demand rather than only reactively.

Reads ``context.portfolio_engine`` directly — the same documented,
narrow, read-only ``PluginContext`` exception ``/analyze`` uses for
``evidence_aggregator``/``reasoning_engine`` (see
``app/plugins/base.py``'s docstring). Never mutates it.
"""
from __future__ import annotations

from typing import Any

from app.discord.actions import ACTION_REGISTRY
from app.discord.command_plugin import DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission
from app.portfolio.models import SymbolProfile

log = get_logger(__name__)

_ACTIONS = ["refresh", "dismiss"]

#: Breakdown keys rendered inline per symbol, in a fixed, readable order —
#: mirrors the breakdown dict app/portfolio/scoring.py::compute_priority()
#: actually returns.
_BREAKDOWN_ORDER = (
    "evidence_strength",
    "fundamental_freshness",
    "context_intensity",
    "confidence_trend",
    "strategy_match",
)


class WatchlistPlugin(DiscordCommandPlugin):
    """Reports the Portfolio Intelligence Layer's current ranked watchlist."""

    name = "Watchlist"
    version = "0.1.0"
    category = "commands"
    command_name = "watchlist"
    command_description = "Show the Portfolio Intelligence Layer's ranked, prioritized watchlist."

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._invocations = 0

    async def initialize(self) -> None:
        log.info("watchlist_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("watchlist_plugin_shutdown", invocations=self._invocations)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._invocations} invocation(s)")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        self._invocations += 1

        portfolio_engine = self.context.portfolio_engine
        if portfolio_engine is None:
            return CommandResponse(
                content="The watchlist isn't available right now — the Portfolio Intelligence Layer isn't wired up.",
                ephemeral=True,
            )

        profiles = portfolio_engine.ranked_watchlist()
        content = _format_watchlist(profiles)
        return CommandResponse(content=content, buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target="watchlist"))


def _format_watchlist(profiles: list[SymbolProfile]) -> str:
    if not profiles:
        return "**Watchlist**\n\nNo symbols are currently configured (see `portfolio.watchlist` in config)."

    lines = ["**Watchlist** _(ranked by priority score)_", ""]
    for rank, profile in enumerate(profiles, start=1):
        trend_arrow = {"rising": "up", "falling": "down", "stable": "stable", "unknown": "unknown"}.get(
            profile.confidence_trend, profile.confidence_trend
        )
        lines.append(f"**{rank}. {profile.symbol}** — priority {profile.priority_score:.0f}/100 _(trend: {trend_arrow})_")

        evidence_bits = (
            f"{profile.active_evidence_count} active evidence "
            f"({profile.bullish_count} bullish, {profile.bearish_count} bearish, {profile.neutral_count} neutral), "
            f"top weight {profile.top_weight:.2f}"
        )
        lines.append(f"  {evidence_bits}")

        if profile.matched_strategies:
            lines.append(f"  Matched strategies: {', '.join(profile.matched_strategies)}")

        if profile.context:
            context_bits = ", ".join(profile.context.values())
            lines.append(f"  Context: {context_bits}")

        if profile.alert_count:
            lines.append(f"  Alerted {profile.alert_count} time(s), most recently {profile.last_alert_at}")

        breakdown_bits = ", ".join(
            f"{key}={profile.priority_breakdown[key]:.1f}"
            for key in _BREAKDOWN_ORDER
            if key in profile.priority_breakdown
        )
        if breakdown_bits:
            lines.append(f"  _Score breakdown: {breakdown_bits}_")

        lines.append("")

    return "\n".join(lines).rstrip()
