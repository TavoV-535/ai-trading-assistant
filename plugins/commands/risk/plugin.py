"""
/risk [profile] — the Capital Protection Engine's current status.

Two modes, both against the same command:

- **Read** (no ``profile`` given): renders the current, continuously
  evolving capital-protection snapshot — equity, daily/total/trailing
  drawdown, consecutive losses, open portfolio risk, position/symbol/
  sector concentration, correlated exposure, the margin/broker
  placeholders (always inapplicable today, shown honestly as such), and
  prop firm compliance — each with its most recently evaluated severity,
  value, and the active Risk Profile's threshold. This is "Demonstrate
  /risk displaying current capital protection status" from the Milestone
  11 spec's completion checklist, made reachable from Discord.
- **Write** (``profile`` given): switches the active Risk Profile via
  ``capital_protection_engine.set_active_profile()`` — every subsequent
  evaluation immediately uses the new profile's thresholds. This is
  "Demonstrate profile switching without code modifications": the engine
  never restarts, no config file is edited, no code changes — a live
  command call is the entire mechanism. An unknown profile name is
  reported back with the list of what's actually configured, never
  silently ignored.

Reads ``context.capital_protection_engine`` directly — the same
documented, narrow, read-only ``PluginContext`` exception ``/analyze``,
``/watchlist``, and ``/journal`` already use (see ``app/plugins/base.py``'s
docstring). ``set_active_profile()`` is the one write-shaped exception,
and even that only ever switches *which already-configured profile* is
active — it never edits a limit, never blocks a trade, and never mutates
anything the Capital Protection Engine itself didn't already expose as a
supported operation.
"""
from __future__ import annotations

from typing import Any

from app.discord.actions import ACTION_REGISTRY
from app.discord.command_plugin import CommandOption, DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.capital_protection.models import CapitalProtectionStatus
from app.event_bus.events import RISK_TYPES, RiskEvent
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission

log = get_logger(__name__)

_ACTIONS = ["refresh", "dismiss"]

_SEVERITY_LABEL = {"info": "OK", "warning": "WARNING", "critical": "BREACH"}


class RiskPlugin(DiscordCommandPlugin):
    """Reads the Capital Protection Engine's current status, or switches
    the active Risk Profile."""

    name = "Risk"
    version = "0.1.0"
    category = "commands"
    command_name = "risk"
    command_description = "Show current capital protection status, or switch the active Risk Profile."
    parameters = (
        CommandOption(
            name="profile",
            description="Switch the active Risk Profile (e.g. conservative, swing_trader, day_trader, scalper, prop_firm)",
            required=False,
        ),
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._invocations = 0

    async def initialize(self) -> None:
        log.info("risk_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("risk_plugin_shutdown", invocations=self._invocations)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._invocations} invocation(s)")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        self._invocations += 1

        engine = self.context.capital_protection_engine
        if engine is None:
            return CommandResponse(
                content="Capital protection status isn't available right now — the Capital Protection Engine isn't wired up.",
                ephemeral=True,
            )

        profile = ctx.args.get("profile")
        profile = str(profile).strip() if profile else ""

        if profile:
            switched = engine.set_active_profile(profile)
            if not switched:
                available = ", ".join(engine.profile_names())
                return CommandResponse(
                    content=f"Unknown Risk Profile '{profile}'. Available profiles: {available}.",
                    ephemeral=True,
                )
            return CommandResponse(content=f"Active Risk Profile switched to '{profile}'.", ephemeral=True)

        status = engine.status()
        return CommandResponse(content=_format_status(status), buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target="risk"))


def _format_status(status: CapitalProtectionStatus) -> str:
    if not status.enabled:
        return "**Capital protection status**\n\nThe Capital Protection Engine is currently disabled (`capital_protection.enabled: false`)."

    lines = [
        f"**Capital protection status** _(profile: {status.active_profile})_",
        "",
        f"Equity: {status.equity:,.2f} _(peak {status.peak_equity:,.2f})_",
        f"Open positions: {status.open_position_count} _(notional {status.open_position_notional:,.2f})_",
        "",
    ]

    for risk_type in RISK_TYPES:
        events = _events_for(status, risk_type)
        if not events:
            lines.append(f"**{risk_type}**: not yet evaluated")
            continue
        for key, event in events:
            lines.append(f"**{key}**: {_format_event(event)}")

    lines.append("")
    lines.append(f"Available profiles: {', '.join(status.profile_names)}")
    lines.append("Switch with `/risk profile:<name>`.")

    return "\n".join(lines)


def _events_for(status: CapitalProtectionStatus, risk_type: str) -> list[tuple[str, RiskEvent]]:
    matches = [(key, event) for key, event in status.latest_risk_events.items() if key == risk_type or key.startswith(f"{risk_type}:")]
    return sorted(matches, key=lambda kv: kv[0])


def _format_event(event: RiskEvent) -> str:
    if not event.applicable:
        return f"n/a — {event.message}"
    label = _SEVERITY_LABEL.get(event.severity, event.severity)
    threshold_bit = f" / limit {event.threshold:.2f}" if event.threshold is not None else ""
    return f"{label} — {event.value:.2f}{threshold_bit} — {event.message}"
