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
from app.plugins.base import PluginBase, PluginCapabilities, PluginContext, PluginHealth
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
        market_data_service: Any | None = None,
        context_engine: Any | None = None,
        portfolio_engine: Any | None = None,
        trading_journal: Any | None = None,
        capital_protection_engine: Any | None = None,
        knowledge_graph: Any | None = None,
        knowledge_graph_query: Any | None = None,
        analytics_service: Any | None = None,
        learning_engine: Any | None = None,
        memory_index: Any | None = None,
        event_replay_service: Any | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._settings = settings
        # Passed straight through to every PluginContext this registry
        # builds — see PluginContext's docstring for why these (and only
        # these) exist as a direct, non-event-bus reference. Also passes
        # itself (``plugin_registry=self``) so a command plugin can
        # introspect what's currently loaded (e.g. ``/scan``'s status
        # report) without a second wiring mechanism.
        self._reasoning_engine = reasoning_engine
        self._evidence_aggregator = evidence_aggregator
        self._strategy_engine = strategy_engine
        self._context_engine = context_engine
        self._portfolio_engine = portfolio_engine
        self._trading_journal = trading_journal
        self._capital_protection_engine = capital_protection_engine
        self._knowledge_graph = knowledge_graph
        self._knowledge_graph_query = knowledge_graph_query
        self._analytics_service = analytics_service
        self._learning_engine = learning_engine
        self._memory_index = memory_index
        self._event_replay_service = event_replay_service
        # Mutable, not constructor-only: market data provider plugins load
        # in an earlier phase than everything else (see bootstrap.py), so
        # this starts as None and is set once the Market Data Abstraction
        # Layer is actually built from those providers, before the second
        # load_all() phase (scanners, indicators, commands) runs.
        self._market_data_service = market_data_service
        self._plugins: dict[str, PluginBase] = {}
        self._failed: dict[str, str] = {}

    def set_market_data_service(self, market_data_service: Any) -> None:
        """Called once by bootstrap() after the Market Data Abstraction
        Layer is constructed from the provider plugins loaded in phase
        one — every PluginContext built by phase two onward sees it."""
        self._market_data_service = market_data_service

    @property
    def plugins(self) -> dict[str, PluginBase]:
        return dict(self._plugins)

    @property
    def failed(self) -> dict[str, str]:
        """Plugin name -> error message, for plugins that failed to load/initialize."""
        return dict(self._failed)

    async def load_all(self, project_root: Path, *, search_paths: list[str] | None = None) -> None:
        """Discover every plugin under the configured search paths and initialize it.

        ``search_paths`` overrides ``settings.plugins.search_paths`` for
        this call only — used by ``bootstrap()`` to load market data
        provider plugins in an isolated first pass, before the Market Data
        Abstraction Layer (which needs those provider instances to exist)
        is built. Calling this more than once is safe: plugins already in
        ``self._plugins`` are untouched, and a name collision across calls
        is logged and skipped exactly like a collision within one call.
        """
        paths = search_paths if search_paths is not None else self._settings.plugins.search_paths
        discovered = discover_plugins(
            search_paths=paths,
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
                market_data_service=self._market_data_service,
                plugin_registry=self,
                context_engine=self._context_engine,
                portfolio_engine=self._portfolio_engine,
                trading_journal=self._trading_journal,
                capital_protection_engine=self._capital_protection_engine,
                knowledge_graph=self._knowledge_graph,
                knowledge_graph_query=self._knowledge_graph_query,
                analytics_service=self._analytics_service,
                learning_engine=self._learning_engine,
                memory_index=self._memory_index,
                event_replay_service=self._event_replay_service,
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

    def capabilities_all(self) -> dict[str, PluginCapabilities]:
        """Every loaded plugin's declared :class:`PluginCapabilities`, by
        name. The Milestone 12 "plugin capability metadata" recommendation
        in practice: a future scanner, router, or command calls this (or
        :meth:`supporting`) to filter plugins by evidence type, market
        type, symbol, or timeframe — without this registry or
        ``PluginBase`` ever needing to know what any specific plugin is."""
        return {name: plugin.capabilities() for name, plugin in self._plugins.items()}

    def supporting(
        self,
        *,
        evidence_type: str | None = None,
        market_type: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> list[PluginBase]:
        """Every loaded plugin whose declared capabilities are compatible
        with every criterion given (see
        :meth:`PluginCapabilities.supports`). Omit a criterion to leave it
        unconstrained. Plugins that never override ``capabilities()``
        declare everything unspecified, so they match any query — capability
        filtering only ever narrows plugins that have actually opted in."""
        return [
            plugin
            for plugin in self._plugins.values()
            if plugin.capabilities().supports(
                evidence_type=evidence_type, market_type=market_type, symbol=symbol, timeframe=timeframe
            )
        ]
