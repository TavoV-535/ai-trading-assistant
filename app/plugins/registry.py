"""
The Plugin Registry.

Turns discovered plugin classes into running plugins: instantiates each one
with a :class:`~app.plugins.base.PluginContext`, calls ``initialize()``, and
tracks them for health checks and graceful shutdown.

A plugin that fails to initialize is isolated — it's logged and excluded,
but it never takes the rest of the application down with it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.event_bus.bus import EventBus
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginContext, PluginHealth
from app.plugins.loader import discover_plugins

log = get_logger(__name__)


class PluginRegistry:
    """Owns the lifecycle of every loaded plugin."""

    def __init__(
        self,
        event_bus: EventBus,
        settings: Any,
        *,
        reasoning_engine: Any | None = None,
        evidence_aggregator: Any | None = None,
        strategy_engine: Any | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._settings = settings
        # Passed straight through to every PluginContext this registry
        # builds — see PluginContext's docstring for why these three (and
        # only these three) exist as a direct, non-event-bus reference.
        self._reasoning_engine = reasoning_engine
        self._evidence_aggregator = evidence_aggregator
        self._strategy_engine = strategy_engine
        self._plugins: dict[str, PluginBase] = {}
        self._failed: dict[str, str] = {}

    @property
    def plugins(self) -> dict[str, PluginBase]:
        return dict(self._plugins)

    @property
    def failed(self) -> dict[str, str]:
        """Plugin name -> error message, for plugins that failed to load/initialize."""
        return dict(self._failed)

    async def load_all(self, project_root: Path) -> None:
        """Discover every plugin under the configured search paths and initialize it."""
        discovered = discover_plugins(
            search_paths=self._settings.plugins.search_paths,
            project_root=project_root,
            disabled=self._settings.plugins.disabled,
        )

        for item in discovered:
            plugin_name = item.plugin_class.name
            if plugin_name in self._plugins:
                log.warning("plugin_name_collision", plugin=plugin_name, path=str(item.module_path))
                continue

            context = PluginContext(
                event_bus=self._event_bus,
                settings=self._settings,
                plugin_config=item.plugin_config,
                reasoning_engine=self._reasoning_engine,
                evidence_aggregator=self._evidence_aggregator,
                strategy_engine=self._strategy_engine,
            )

            try:
                plugin = item.plugin_class(context)
                await plugin.initialize()
            except Exception as exc:
                log.exception("plugin_initialize_failed", plugin=plugin_name)
                self._failed[plugin_name] = str(exc)
                continue

            self._plugins[plugin_name] = plugin
            log.info(
                "plugin_initialized",
                plugin=plugin_name,
                category=item.category,
                version=plugin.version,
            )

        log.info(
            "plugin_registry_ready",
            loaded=len(self._plugins),
            failed=len(self._failed),
        )

    async def shutdown_all(self) -> None:
        for name, plugin in self._plugins.items():
            try:
                await plugin.shutdown()
                log.info("plugin_shutdown", plugin=name)
            except Exception:
                log.exception("plugin_shutdown_failed", plugin=name)

    async def health_check_all(self) -> dict[str, PluginHealth]:
        results: dict[str, PluginHealth] = {}
        for name, plugin in self._plugins.items():
            try:
                results[name] = await plugin.health()
            except Exception as exc:
                log.exception("plugin_health_check_failed", plugin=name)
                results[name] = PluginHealth(status="unhealthy", detail=str(exc))
        return results

    def get(self, name: str) -> PluginBase | None:
        return self._plugins.get(name)
