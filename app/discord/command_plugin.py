"""
The command-plugin contract.

A Discord slash command is a plugin, exactly like an indicator or a
scanner. Drop a folder under ``plugins/commands/``, implement
``DiscordCommandPlugin``, and it's auto-discovered and registered as a
``/slash-command`` the next time the bot starts — no core code changes, no
manual registration step.
"""
from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass

from app.discord.dispatch import CommandContext, CommandResponse
from app.plugins.base import PluginBase

# Discord's real rule is more permissive (allows some unicode), but this
# ASCII-only subset is a safe, simple check we can validate at load time
# without needing Discord's own name-validation library. The same rule
# applies to option names, so it's reused for both.
_VALID_COMMAND_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


def is_valid_command_name(name: str) -> bool:
    return bool(_VALID_COMMAND_NAME.match(name))


def is_valid_option_name(name: str) -> bool:
    return bool(_VALID_COMMAND_NAME.match(name))


@dataclass(frozen=True)
class CommandOption:
    """One parameter a slash command declares, e.g. ``symbol`` for
    ``/analyze SYMBOL``.

    Currently string-only — every declared option becomes a Discord string
    option (see ``app/discord/bot.py``'s ``_build_parameterized_callback``,
    which derives the real discord.py option from this data). Extend this
    dataclass with an ``option_type`` field (and teach the bot how to
    generate int/float/bool/choice signatures) before a command needs
    anything other than a string.
    """

    name: str
    description: str
    required: bool = True


class DiscordCommandPlugin(PluginBase):
    """Base class for every ``/command`` plugin.

    Adds two things on top of the Universal Plugin Contract:

    - ``execute()``, which receives a :class:`~app.discord.dispatch.CommandContext`
      and returns a :class:`~app.discord.dispatch.CommandResponse` — never
      the raw discord.py ``Interaction``, so command plugins stay testable
      without a live Discord connection.
    - ``parameters``, a declarative list of :class:`CommandOption` the
      command takes (empty by default — a zero-parameter command like
      ``/ping`` or ``/help``). Declaring a parameter here is the entire
      integration step; ``TradingBot`` builds the real discord.py slash
      command option from it, with no core code changes needed elsewhere.
    """

    category: str = "commands"

    #: The literal slash command name, e.g. "ping" for ``/ping``.
    command_name: str = "unnamed-command"
    command_description: str = ""

    #: Declared slash-command parameters, in declaration order. A tuple, not
    #: a list, so a subclass overriding this at the class level never
    #: accidentally shares a mutable default across instances.
    parameters: tuple[CommandOption, ...] = ()

    @abstractmethod
    async def execute(self, ctx: CommandContext) -> CommandResponse:
        """Handle the command and return the response to send back.

        ``ctx.args`` is keyed by each declared :class:`CommandOption`'s
        ``name`` — e.g. ``ctx.args["symbol"]`` for a command declaring
        ``CommandOption(name="symbol", ...)``.
        """
