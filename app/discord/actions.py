"""
The Discord Action Registry.

Milestone 5 gave every command the ability to attach buttons
(``CommandResponse.buttons``), but left each command building its own
``CommandButton`` list and left ``TradingBot`` owning the one-size-fits-all
"dismiss vs. not-built-yet" click behavior directly. That doesn't scale:
every future command that wants a Chart or Watch button would either
duplicate that logic or drift from it.

This registry centralizes the *reusable* Discord actions
(chart/news/history/backtest/journal/watch/refresh/replay/coach/dismiss)
so a command plugin declares *which* actions it wants —

    ACTION_REGISTRY.buttons_for(["chart", "news", "watch", "dismiss"], target=symbol)

— instead of constructing ``CommandButton`` objects and reimplementing
click behavior. The registry owns:

- **Button creation** — ``buttons_for()`` turns action keys into
  ``CommandButton``s with consistent labels/styles.
- **Callback registration** — ``to_discord_button()`` (called only from
  ``app/discord/bot.py``, the one place allowed to touch real discord.py
  objects) wires each button's real click handler.
- **Shared styling** — one ``ActionDefinition`` per action, reused by
  every command that asks for it.
- **Placeholder behavior** — any action without a real handler registered
  yet gets an honest "not built yet" reply, generic and consistent across
  every command, not hand-rolled per command.
- **Future routing / future implementations** — a real Chart/Watch/etc.
  handler is registered here once (``register_handler``) and every
  command that already asks for that action key gets the real behavior
  for free, with no command-plugin changes.
- **Permission handling** — ``ActionDefinition.requires_permission`` is a
  documented seam for a future role/permission system. Nothing in this
  codebase enforces Discord roles or user permissions yet, so this is
  currently a no-op that always allows the action — it exists so that
  seam doesn't require another refactor later, not to pretend enforcement
  exists today.

``custom_id`` convention: ``"{action_key}:{target}"`` (e.g.
``"chart:NVDA"``) — deliberately action-first and command-agnostic, so
the same button behaves identically regardless of which command attached
it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.discord.dispatch import CommandButton
from app.logging import get_logger

log = get_logger(__name__)

# Receives the real discord.Interaction and the button's "target" (the
# custom_id's segment after the action key, e.g. a symbol) and does
# whatever the action requires (send a reply, delete the message, ...).
# Imported lazily / typed loosely here (Any-ish via TYPE_CHECKING) so this
# module doesn't need a hard discord.py import just to define the type.
ActionHandler = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class ActionDefinition:
    key: str
    label: str
    style: str = "secondary"  # "primary" | "secondary" | "success" | "danger"
    #: Not enforced anywhere yet — no role/permission system exists in this
    #: codebase. A documented seam for a future one, not a live check.
    requires_permission: str | None = None


async def _dismiss_handler(interaction: Any, target: str) -> None:
    """The one action with real behavior today. Deletes the message the
    button is attached to; if there's somehow no message to delete (a
    stale/duck-typed interaction in a test), falls back to an ephemeral
    acknowledgement instead of raising.

    Written against duck-typed ``interaction``/``interaction.message``
    attributes rather than importing ``discord`` — this module stays
    usable and testable without a discord.py dependency, exactly like
    ``app/discord/dispatch.py``.
    """
    message = getattr(interaction, "message", None)
    if message is not None:
        await message.delete()
    else:
        await interaction.response.send_message("Dismissed.", ephemeral=True)


def _make_placeholder_handler(label: str) -> ActionHandler:
    """Every action without a real handler registered gets this — an
    honest, generic reply, not a silent no-op and not a fabricated
    result."""

    async def _handler(interaction: Any, target: str) -> None:
        await interaction.response.send_message(
            f"'{label}' isn't built yet — see docs/MILESTONES.md for the roadmap.",
            ephemeral=True,
        )

    return _handler


class ActionRegistry:
    """Process-wide registry of known Discord button actions. Not a
    plugin — there's exactly one, imported directly (the same way command
    plugins already import ``app.discord.dispatch`` directly; this is part
    of the Discord command contract, not "another plugin's module")."""

    def __init__(self) -> None:
        self._definitions: dict[str, ActionDefinition] = {}
        self._handlers: dict[str, ActionHandler] = {}
        self._implemented: set[str] = set()
        self._register_builtin_actions()

    # ---------------------------------------------------------------- registration

    def register(self, definition: ActionDefinition, handler: ActionHandler | None = None) -> None:
        """Add or replace an action definition. An action always has
        *some* handler from the moment it's registered — a real one if
        given, otherwise the generic placeholder — so :meth:`handler_for`
        never returns ``None`` for a known key."""
        self._definitions[definition.key] = definition
        self._handlers[definition.key] = handler or _make_placeholder_handler(definition.label)
        if handler is not None:
            self._implemented.add(definition.key)

    def register_handler(self, key: str, handler: ActionHandler) -> None:
        """Give an already-registered action key real behavior. This is
        the seam future milestones use to make Chart/News/History/
        Backtest/Journal/Watch/Replay/Coach do something real — every
        command already asking for that action key picks up the new
        behavior automatically, no command-plugin changes required."""
        if key not in self._definitions:
            log.warning("action_handler_registered_for_unknown_key", key=key)
            return
        self._handlers[key] = handler
        self._implemented.add(key)

    def _register_builtin_actions(self) -> None:
        for definition in (
            ActionDefinition("chart", "Chart"),
            ActionDefinition("news", "News"),
            ActionDefinition("history", "History"),
            ActionDefinition("backtest", "Backtest"),
            ActionDefinition("journal", "Journal"),
            ActionDefinition("watch", "Watch"),
            ActionDefinition("refresh", "Refresh"),
            ActionDefinition("replay", "Replay"),
            ActionDefinition("coach", "Coach"),
            ActionDefinition("dismiss", "Dismiss", style="danger"),
        ):
            self.register(definition)
        self.register_handler("dismiss", _dismiss_handler)

    # ---------------------------------------------------------------- queries

    def definition(self, key: str) -> ActionDefinition | None:
        return self._definitions.get(key)

    def handler_for(self, key: str) -> ActionHandler | None:
        """The real handler if one's registered, otherwise the generic
        placeholder — ``None`` only for a key that was never registered
        via :meth:`register` at all."""
        return self._handlers.get(key)

    def is_implemented(self, key: str) -> bool:
        """Whether ``key`` has a deliberately-registered real handler, as
        opposed to the generic placeholder every registered action starts
        with."""
        return key in self._implemented

    def buttons_for(self, action_keys: list[str], target: str) -> list[CommandButton]:
        """Turn a list of action keys into real ``CommandButton``s. An
        unknown key is logged and skipped — the same isolation policy
        every other lookup-by-name in this codebase uses (an invalid
        command/option name is skipped, not fatal; this is no different).
        """
        buttons: list[CommandButton] = []
        for key in action_keys:
            definition = self._definitions.get(key)
            if definition is None:
                log.warning("unknown_action_key_requested", key=key)
                continue
            buttons.append(
                CommandButton(label=definition.label, custom_id=f"{key}:{target}", style=definition.style)
            )
        return buttons

    @staticmethod
    def parse_custom_id(custom_id: str) -> tuple[str, str]:
        """``"chart:NVDA"`` -> ``("chart", "NVDA")``. A target-less
        custom_id (no ``":"``) yields an empty target rather than
        raising."""
        key, _, target = custom_id.partition(":")
        return key, target

    def check_permission(self, key: str) -> bool:
        """Always ``True`` today — see ``ActionDefinition.requires_permission``'s
        docstring. Called from ``app/discord/bot.py`` before running a
        handler so enforcing it later is a one-line change here, not a
        new integration point."""
        return True


#: Process-wide singleton — the same registry every command plugin and
#: ``TradingBot`` shares, so a handler registered once is visible
#: everywhere.
ACTION_REGISTRY = ActionRegistry()
