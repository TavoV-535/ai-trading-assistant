"""Bundles the process-wide singletons every part of the app shares."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.base import Database
from app.event_bus.bus import EventBus
from app.plugins.registry import PluginRegistry
from app.reasoning.engine import ReasoningEngine


@dataclass
class AppState:
    settings: Any
    event_bus: EventBus
    database: Database
    plugin_registry: PluginRegistry
    reasoning_engine: ReasoningEngine
    project_root: Path
