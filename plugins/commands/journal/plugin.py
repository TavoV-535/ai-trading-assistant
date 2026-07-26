"""
/journal SYMBOL [note] — the Trading Journal's enriched, retrievable
history for a symbol.

Two modes, both against the same command:

- **Read** (no ``note`` given): renders every :class:`JournalEntry` the
  Trading Journal currently holds for ``symbol`` — each one a Decision
  Timeline record (Milestone 9) enriched with its Reflection Engine
  analysis (Milestone 10: why the decision was made, supporting/
  contradictory evidence, market context, confidence evolution, outcome,
  lessons learned, potential improvements), plus any user notes and
  screenshot placeholders attached to it — followed by any general notes/
  screenshots recorded for the symbol that aren't tied to a specific
  decision. This is "retrieve the complete historical decision record"
  from the Milestone 10 spec's completion checklist, made reachable from
  Discord.
- **Write** (``note`` given): appends a user note. If the symbol has at
  least one journal entry, the note attaches to the most recent one
  (``decision_event_id`` supplied); otherwise it's recorded as a general,
  symbol-level note (``decision_event_id`` omitted) — both are valid, per
  ``JournalCreated``'s own docstring. Either way this command never
  mutates ``context.trading_journal`` directly — it calls
  ``trading_journal.add_note()``, which only ever works by publishing a
  ``JournalCreated`` event that the Journal's own subscriber reacts to,
  the same event-only mutation rule every other write path in this
  codebase follows (see ``app/journal/engine.py``).

Screenshot upload isn't a real capability yet (placeholder support only,
per the Milestone 10 spec) — this command doesn't expose a way to attach
one; ``trading_journal.add_screenshot()`` exists for a future integration
(e.g. an attachment-aware Discord command) to call.

Reads ``context.trading_journal`` directly — the same documented, narrow,
read-only ``PluginContext`` exception ``/analyze`` and ``/watchlist``
already use (see ``app/plugins/base.py``'s docstring). The one "write"
capability, ``add_note()``, still only mutates via an event the Journal
reacts to itself — never a direct in-memory mutation from this plugin.

The Action Registry's pre-existing "journal" button (attached to
``/analyze``'s response — see ``app/discord/actions.py``) intentionally
still uses the generic "not built yet" placeholder handler: a button
callback only receives ``(interaction, target)``, with no
``PluginContext``/live-engine access, so wiring a real handler for it
would need a larger structural change this milestone's spec didn't ask
for. This command is the supported way to reach the Journal today.
"""
from __future__ import annotations

from typing import Any

from app.discord.actions import ACTION_REGISTRY
from app.discord.command_plugin import CommandOption, DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.journal.models import JournalEntry, JournalNote
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission

log = get_logger(__name__)

_ACTIONS = ["refresh", "dismiss"]

#: Most recent entries rendered per read -- keeps the message from growing
#: unbounded for a symbol with a long history; the full, unbounded history
#: always remains queryable via EventLogRepository.decision_records() /
#: .reflections() / .journal_notes() directly against the database.
_MAX_ENTRIES_RENDERED = 5


class JournalPlugin(DiscordCommandPlugin):
    """Reads and appends to the Trading Journal's enriched history for a symbol."""

    name = "Journal"
    version = "0.1.0"
    category = "commands"
    command_name = "journal"
    command_description = "View a symbol's journal history, or add a note with the optional `note` parameter."
    parameters = (
        CommandOption(name="symbol", description="Ticker symbol, e.g. NVDA", required=True),
        CommandOption(name="note", description="Optional note to record against this symbol", required=False),
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._invocations = 0

    async def initialize(self) -> None:
        log.info("journal_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("journal_plugin_shutdown", invocations=self._invocations)

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
            return CommandResponse(content="Usage: `/journal SYMBOL [note]`, e.g. `/journal NVDA`.", ephemeral=True)

        trading_journal = self.context.trading_journal
        if trading_journal is None:
            return CommandResponse(
                content="The journal isn't available right now — the Trading Journal isn't wired up.",
                ephemeral=True,
            )

        note = ctx.args.get("note")
        note = str(note).strip() if note else ""

        if note:
            return await self._add_note(trading_journal, symbol=symbol, text=note, author=ctx.user_id)

        entries = trading_journal.for_symbol(symbol)
        general_notes = trading_journal.general_notes_for(symbol)
        general_screenshots = trading_journal.general_screenshots_for(symbol)
        content = _format_journal(symbol, entries, general_notes, general_screenshots)
        return CommandResponse(content=content, buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target=symbol))

    async def _add_note(self, trading_journal: Any, *, symbol: str, text: str, author: str) -> CommandResponse:
        existing = trading_journal.for_symbol(symbol)
        decision_event_id = existing[-1].decision_event_id if existing else None
        await trading_journal.add_note(symbol=symbol, text=text, decision_event_id=decision_event_id, author=author)

        if decision_event_id is not None:
            confirmation = f"Noted against {symbol}'s most recent journal entry ({decision_event_id})."
        else:
            confirmation = f"Noted against {symbol} — no journal entries exist yet, so this was recorded as a general note."
        return CommandResponse(content=confirmation, ephemeral=True)


def _format_journal(
    symbol: str,
    entries: list[JournalEntry],
    general_notes: list[JournalNote],
    general_screenshots: list[str],
) -> str:
    if not entries and not general_notes and not general_screenshots:
        return (
            f"**{symbol} journal**\n\nNo journal entries yet — the Trading Journal enriches "
            "Decision Timeline records as they're produced (today: by the Simulation Engine; "
            "see docs/MILESTONES.md for live-mode scope)."
        )

    lines = [f"**{symbol} journal** _({len(entries)} entrie(s))_", ""]

    for entry in entries[-_MAX_ENTRIES_RENDERED:]:
        decision = entry.decision
        lines.append(f"**{decision.timestamp:%Y-%m-%d %H:%M}** — {decision.simulated_action} _(confidence {decision.confidence:.0f}/100)_")
        if decision.outcome_pending:
            lines.append("  Outcome: pending")
        elif decision.outcome is not None:
            change = (
                f", {decision.outcome_price_change_pct:+.2f}%" if decision.outcome_price_change_pct is not None else ""
            )
            lines.append(f"  Outcome: {decision.outcome}{change}")
        if decision.strategy_matches:
            lines.append(f"  Matched strategies: {', '.join(decision.strategy_matches)}")
        if decision.market_context:
            lines.append(f"  Context: {', '.join(decision.market_context.values())}")

        reflection = entry.reflection
        if reflection is not None:
            lines.append(f"  Reasoning: {reflection.reasoning}")
            if reflection.supporting_evidence:
                lines.append(f"  Supporting: {'; '.join(reflection.supporting_evidence)}")
            if reflection.contradictory_evidence:
                lines.append(f"  Contradictory: {'; '.join(reflection.contradictory_evidence)}")
            lines.append(f"  Confidence evolution: {reflection.confidence_evolution}")
            lines.append(f"  Lessons learned: {reflection.lessons_learned}")
            lines.append(f"  Potential improvements: {reflection.potential_improvements}")
        else:
            lines.append("  Reflection: not yet generated.")

        if entry.notes:
            notes_bits = "; ".join(n.text for n in entry.notes)
            lines.append(f"  Notes ({len(entry.notes)}): {notes_bits}")
        if entry.screenshots:
            lines.append(f"  Screenshots: {len(entry.screenshots)} attached")
        if entry.broker_execution is not None:
            lines.append(f"  Broker execution: {entry.broker_execution}")

        lines.append("")

    if len(entries) > _MAX_ENTRIES_RENDERED:
        lines.append(f"_...and {len(entries) - _MAX_ENTRIES_RENDERED} earlier entrie(s), oldest first above cut off._")
        lines.append("")

    if general_notes:
        lines.append(f"**General notes** ({len(general_notes)}): " + "; ".join(n.text for n in general_notes))
    if general_screenshots:
        lines.append(f"**General screenshots**: {len(general_screenshots)} attached")

    return "\n".join(lines).rstrip()
