"""
Tests for the Discord Action Registry (app/discord/actions.py).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.discord.actions import ActionDefinition, ActionRegistry
from app.discord.dispatch import CommandButton


def _fake_interaction(with_message: bool = True):
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    if with_message:
        interaction.message.delete = AsyncMock()
    else:
        interaction.message = None
    return interaction


@pytest.fixture
def registry() -> ActionRegistry:
    return ActionRegistry()


def test_buttons_for_builds_commandbuttons_with_action_first_custom_id(registry):
    buttons = registry.buttons_for(["chart", "dismiss"], target="NVDA")
    assert buttons == [
        CommandButton(label="Chart", custom_id="chart:NVDA", style="secondary"),
        CommandButton(label="Dismiss", custom_id="dismiss:NVDA", style="danger"),
    ]


def test_buttons_for_skips_unknown_action_keys(registry):
    buttons = registry.buttons_for(["chart", "not-a-real-action", "dismiss"], target="NVDA")
    assert [b.custom_id for b in buttons] == ["chart:NVDA", "dismiss:NVDA"]


def test_parse_custom_id_splits_action_and_target(registry):
    assert registry.parse_custom_id("chart:NVDA") == ("chart", "NVDA")
    assert registry.parse_custom_id("dismiss:scan") == ("dismiss", "scan")


def test_parse_custom_id_handles_missing_target(registry):
    assert registry.parse_custom_id("chart") == ("chart", "")


def test_every_builtin_action_has_some_handler(registry):
    for key in ("chart", "news", "history", "backtest", "journal", "watch", "refresh", "replay", "coach", "dismiss"):
        assert registry.handler_for(key) is not None


def test_only_dismiss_is_implemented_by_default(registry):
    assert registry.is_implemented("dismiss") is True
    for key in ("chart", "news", "history", "backtest", "journal", "watch", "refresh", "replay", "coach"):
        assert registry.is_implemented(key) is False


async def test_dismiss_handler_deletes_the_message(registry):
    interaction = _fake_interaction(with_message=True)
    handler = registry.handler_for("dismiss")
    await handler(interaction, "NVDA")
    interaction.message.delete.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


async def test_dismiss_handler_falls_back_when_no_message(registry):
    interaction = _fake_interaction(with_message=False)
    handler = registry.handler_for("dismiss")
    await handler(interaction, "NVDA")
    interaction.response.send_message.assert_awaited_once_with("Dismissed.", ephemeral=True)


async def test_placeholder_handler_sends_honest_not_built_yet_reply(registry):
    interaction = _fake_interaction()
    handler = registry.handler_for("chart")
    await handler(interaction, "NVDA")
    interaction.response.send_message.assert_awaited_once()
    content = interaction.response.send_message.call_args.args[0]
    assert "Chart" in content
    assert "isn't built yet" in content
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_register_handler_gives_a_known_action_real_behavior(registry):
    called_with = []

    async def real_chart_handler(interaction, target):
        called_with.append(target)

    registry.register_handler("chart", real_chart_handler)
    assert registry.is_implemented("chart") is True

    interaction = _fake_interaction()
    await registry.handler_for("chart")(interaction, "NVDA")
    assert called_with == ["NVDA"]
    interaction.response.send_message.assert_not_awaited()  # the real handler didn't send anything


def test_register_handler_for_unknown_key_logs_and_is_a_no_op(registry):
    async def handler(interaction, target):
        pass

    registry.register_handler("not-a-real-action", handler)  # must not raise
    assert registry.handler_for("not-a-real-action") is None


def test_check_permission_always_true_today(registry):
    # No role/permission system exists yet -- documented no-op, not a live check.
    assert registry.check_permission("dismiss") is True
    assert registry.check_permission("chart") is True


def test_register_can_add_a_brand_new_action(registry):
    registry.register(ActionDefinition("coach2", "Coach2", style="primary"))
    buttons = registry.buttons_for(["coach2"], target="x")
    assert buttons == [CommandButton(label="Coach2", custom_id="coach2:x", style="primary")]


async def test_register_with_a_handler_marks_it_implemented_immediately(registry):
    async def real_handler(interaction, target):
        pass

    registry.register(ActionDefinition("live", "Live"), handler=real_handler)
    assert registry.is_implemented("live") is True
    assert registry.handler_for("live") is real_handler


def test_definition_returns_the_registered_definition(registry):
    definition = registry.definition("chart")
    assert definition.key == "chart"
    assert definition.label == "Chart"
    assert registry.definition("not-a-real-action") is None
