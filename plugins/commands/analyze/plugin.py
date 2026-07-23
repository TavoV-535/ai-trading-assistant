"""
/analyze SYMBOL — the first command with a real parameter.

Pulls the Evidence Aggregator's current snapshot and the Reasoning Engine's
current output for a symbol, and renders them as one message with
interactive buttons: Chart / News / History / Backtest / Journal / Watch /
Dismiss.

Buttons come from the Discord Action Registry (``app/discord/actions.py``)
— this plugin declares which actions it wants (``_ACTIONS`` below) and
asks ``ACTION_REGISTRY.buttons_for()`` for the real ``CommandButton``s,
rather than constructing them or implementing click behavior itself.
Only "Dismiss" actually does something today (it deletes the message) —
the other six name systems that don't exist yet (no charting, no news
engine, no history/backtest/journal/watchlist systems — see
docs/MILESTONES.md's "Proposed order for what's next"), so clicking them
gets an honest "not built yet" reply instead of doing nothing silently or
pretending to work. When those systems exist, registering their real
handlers with the Action Registry is enough — this plugin won't change.

This plugin reads ``context.evidence_aggregator`` and
``context.reasoning_engine`` directly — a deliberate, narrow, documented
exception to "plugins only talk through the Event Bus," scoped to
read-only, on-demand queries (see ``PluginContext``'s docstring in
``app/plugins/base.py``). It never mutates either, and never reaches into
a specific indicator plugin's implementation — only the same
``Evidence``/``ReasoningOutput`` vocabulary everything else in this
codebase already speaks.

Known limitation, not a bug: there is no live market data feed yet (that's
the Scanner Engine, also on the roadmap), so the Reasoning Engine has no
evidence for a symbol until *something* has actually published
``MarketDataUpdated`` for it. Until then, ``/analyze`` for any real-world
symbol will honestly report "insufficient evidence" — the same graceful
degradation the Reasoning Engine already uses everywhere else.
"""
from __future__ import annotations

from typing import Any

from app.aggregation.models import AggregateSnapshot
from app.discord.actions import ACTION_REGISTRY
from app.discord.command_plugin import CommandOption, DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission
from app.reasoning.engine import ReasoningOutput

log = get_logger(__name__)

#: Which reusable Discord actions this command wants — see
#: app/discord/actions.py. Order here is the order buttons render in.
_ACTIONS = ["chart", "news", "history", "backtest", "journal", "watch", "dismiss"]


class AnalyzePlugin(DiscordCommandPlugin):
    """Pulls evidence + reasoning output for a symbol."""

    name = "Analyze"
    version = "0.1.0"
    category = "commands"
    command_name = "analyze"
    command_description = "Pull the current evidence and reasoning output for a symbol."
    parameters = (CommandOption(name="symbol", description="Ticker symbol, e.g. NVDA", required=True),)

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._invocations = 0

    async def initialize(self) -> None:
        log.info("analyze_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("analyze_plugin_shutdown", invocations=self._invocations)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._invocations} invocation(s)")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        self._invocations += 1
        symbol = str(ctx.args.get("symbol") or "").strip().upper()
        if not symbol:
            return CommandResponse(content="Usage: `/analyze SYMBOL`, e.g. `/analyze NVDA`.", ephemeral=True)

        aggregator = self.context.evidence_aggregator
        reasoning_engine = self.context.reasoning_engine
        if aggregator is None or reasoning_engine is None:
            log.warning("analyze_missing_core_services", symbol=symbol)
            return CommandResponse(
                content="Analysis isn't available right now — the reasoning system isn't wired up.",
                ephemeral=True,
            )

        snapshot = aggregator.snapshot(symbol)
        output = await reasoning_engine.analyze(symbol)

        return CommandResponse(
            content=_format_analysis(symbol, snapshot, output),
            buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target=symbol),
        )


def _format_analysis(symbol: str, snapshot: AggregateSnapshot, output: ReasoningOutput) -> str:
    lines = [f"**{symbol} analysis** _(source: {output.source})_", "", output.market_summary]

    if output.source != "insufficient_evidence":
        lines.append("")
        lines.append(f"**Thesis:** {output.trade_thesis}")
        lines.append(f"**Risk:** {output.risk_assessment}")
        lines.append(f"**Alternative scenario:** {output.alternative_scenario}")
        lines.append(f"**Confidence:** {output.confidence:.0f}/100")
        if output.suggested_strategies:
            lines.append(f"**Matched strategies:** {', '.join(output.suggested_strategies)}")
        if output.historical_similarity:
            lines.append(f"**Historically similar to:** {output.historical_similarity}")

    conflict_note = " — **conflicting signals present**" if snapshot.has_conflict else ""
    lines.append("")
    lines.append(
        f"Evidence: {len(snapshot.active_evidence)} active "
        f"({snapshot.bullish_count} bullish, {snapshot.bearish_count} bearish, "
        f"{snapshot.neutral_count} neutral){conflict_note}"
    )

    return "\n".join(lines)
