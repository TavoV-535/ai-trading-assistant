"""Bundles the process-wide singletons every part of the app shares."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db.base import Database
from app.event_bus.bus import EventBus
from app.plugins.registry import PluginRegistry
from app.reasoning.engine import ReasoningEngine

if TYPE_CHECKING:
    from app.discord.bot import TradingBot


@dataclass
class AppState:
    settings: Any
    event_bus: EventBus
    database: Database
    plugin_registry: PluginRegistry
    reasoning_engine: ReasoningEngine
    project_root: Path
    discord_bot: "TradingBot | None" = None
    discord_task: "asyncio.Task[None] | None" = None
