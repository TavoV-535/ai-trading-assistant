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

from app.discord.dispatch import CommandContext, CommandResponse
from app.plugins.base import PluginBase

# Discord's real rule is more permissive (allows some unicode), but this
# ASCII-only subset is a safe, simple check we can validate at load time
# without needing Discord's own name-validation library.
_VALID_COMMAND_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


def is_valid_command_name(name: str) -> bool:
    return bool(_VALID_COMMAND_NAME.match(name))


class DiscordCommandPlugin(PluginBase):
    """Base class for every ``/command`` plugin.

    Adds one method on top of the Universal Plugin Contract:
    ``execute()``, which receives a :class:`~app.discord.dispatch.CommandContext`
    and returns a :class:`~app.discord.dispatch.CommandResponse` — never the
    raw discord.py ``Interaction``, so command plugins stay testable without
    a live Discord connection.
    """

    category: str = "commands"

    #: The literal slash command name, e.g. "ping" for ``/ping``.
    command_name: str = "unnamed-command"
    command_description: str = ""

    @abstractmethod
    async def execute(self, ctx: CommandContext) -> CommandResponse:
        """Handle the command and return the response to send back."""
