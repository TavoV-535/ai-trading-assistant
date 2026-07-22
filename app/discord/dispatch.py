"""
Command dispatch — the logic between "a Discord command fired" and "a
command plugin ran" that isn't tied to discord.py's ``Interaction`` object.

Kept deliberately independent of ``discord.Interaction`` so it can run in
unit tests without a live gateway connection. ``app/discord/bot.py`` is the
thin adapter that extracts fields off a real ``Interaction`` and calls
:func:`dispatch_command`; that adapter is the only part of this system that
requires an actual Discord connection to exercise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.event_bus.bus import EventBus
from app.event_bus.events import CommandFailed, CommandInvoked
from app.logging import get_logger

if TYPE_CHECKING:
    from app.discord.command_plugin import DiscordCommandPlugin

log = get_logger(__name__)


@dataclass(frozen=True)
class CommandContext:
    """Everything a command plugin needs to know about the invocation —
    deliberately not the raw discord.py ``Interaction``, so command plugins
    never depend on discord.py internals either."""

    user_id: str
    guild_id: str | None
    channel_id: str | None
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandButton:
    """One interactive button attached to a :class:`CommandResponse`.

    Kept as a plain dataclass — never a real ``discord.ui.Button`` — so a
    command plugin declaring buttons stays testable without discord.py,
    exactly like ``CommandResponse`` itself. ``app/discord/bot.py`` is the
    only place that turns this into a real Discord component.

    ``custom_id`` convention: ``"{command}:{action}:{extra}"``, e.g.
    ``"analyze:chart:NVDA"``. The Discord adapter treats the action segment
    ``"dismiss"`` specially (deletes the message it's attached to); every
    other action currently gets a generic "not built yet" reply, since the
    systems some buttons imply (Chart/News/History/Backtest/Journal/Watch)
    don't exist yet — see docs/MILESTONES.md for the roadmap. This
    convention is generic, not specific to ``/analyze``: any future command
    plugin can reuse the same ``dismiss`` action and get the same behavior
    for free.
    """

    label: str
    custom_id: str
    style: str = "secondary"  # "primary" | "secondary" | "success" | "danger"
    disabled: bool = False


@dataclass(frozen=True)
class CommandResponse:
    content: str
    ephemeral: bool = False
    buttons: list[CommandButton] = field(default_factory=list)


async def dispatch_command(
    plugin: "DiscordCommandPlugin",
    event_bus: EventBus,
    ctx: CommandContext,
) -> CommandResponse:
    """Publish the audit event, run the plugin, publish a failure event and
    return a graceful error response if it raises. Never propagates a
    plugin exception back to the caller — a broken command must not crash
    the bot process, exactly like a broken plugin must not crash boot."""

    await event_bus.publish(
        CommandInvoked(
            source="discord",
            command=plugin.command_name,
            user_id=ctx.user_id,
            guild_id=ctx.guild_id,
            channel_id=ctx.channel_id,
            args=dict(ctx.args),
        )
    )

    try:
        return await plugin.execute(ctx)
    except Exception as exc:
        log.exception("command_execution_failed", command=plugin.command_name, user_id=ctx.user_id)
        await event_bus.publish(
            CommandFailed(source="discord", command=plugin.command_name, user_id=ctx.user_id, error=str(exc))
        )
        return CommandResponse(
            content=f"Something went wrong running `/{plugin.command_name}`. This has been logged.",
            ephemeral=True,
        )
