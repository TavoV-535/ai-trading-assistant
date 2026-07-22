"""
The Universal Plugin Contract.

Every plugin — indicator, strategy, scanner, news source, broker
integration, whatever — implements this same interface. Adding a new
capability means adding a folder under ``/plugins``; it never means editing
core code.

Every plugin must implement:

- ``initialize()``  — acquire resources, subscribe to events
- ``shutdown()``    — release resources cleanly
- ``health()``      — report whether it's working
- ``config()``      — return its current configuration
- ``permissions()`` — declare what it needs access to

Plugins talk to the rest of the system only through the
:class:`~app.event_bus.bus.EventBus` handed to them in their
:class:`PluginContext`. They never import and call another plugin directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from app.event_bus.bus import EventBus

if TYPE_CHECKING:
    from app.aggregation.aggregator import EvidenceAggregator
    from app.reasoning.engine import ReasoningEngine
    from app.strategy.engine import StrategyEngine

HealthStatus = Literal["healthy", "degraded", "unhealthy"]


class PluginPermission:
    """Common permission strings a plugin can declare via :meth:`PluginBase.permissions`.

    Not an enforced sandbox in Milestone 1 — declaring permissions makes a
    plugin's intent legible (to you, to code review, and later to an
    enforcement layer) without hardcoding a fixed permission set.
    """

    EVENTS_PUBLISH = "events.publish"
    EVENTS_SUBSCRIBE = "events.subscribe"
    MARKET_DATA_READ = "market_data.read"
    DB_READ = "db.read"
    DB_WRITE = "db.write"
    NETWORK_OUTBOUND = "network.outbound"
    DISCORD_RESPOND = "discord.respond"
    BROKER_EXECUTE = "broker.execute"


class PluginMetadata(BaseModel):
    name: str
    version: str = "0.1.0"
    description: str = ""
    category: str = "uncategorized"
    author: str | None = None


class PluginHealth(BaseModel):
    status: HealthStatus = "healthy"
    detail: str | None = None
    checked_at: datetime = datetime.now(timezone.utc)

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class PluginContext:
    """Everything a plugin is handed at construction time.

    Plugins reach the rest of the system only through this object — never by
    importing core modules directly. This is what keeps a plugin from
    needing to know about anything outside its own folder.

    ``reasoning_engine``, ``evidence_aggregator``, and ``strategy_engine``
    are a deliberate, narrow exception to "plugins only talk through the
    Event Bus." They exist so a command plugin can answer an on-demand,
    synchronous, read-only query — e.g. ``/analyze NVDA`` needs whatever
    the *current* evidence snapshot and reasoning output are right now, not
    whatever the next event happens to publish. A plugin may read from
    these (``.snapshot()``, ``.analyze()``, ``.matched_strategies_for()``,
    etc.) but must never use them to mutate state, publish on another
    system's behalf, or reach into a specific indicator plugin's internals
    — evidence and events remain the only way to make something happen.
    They default to ``None`` (most unit tests, and any future refactor,
    may not supply them), so any plugin reading them must handle ``None``
    gracefully instead of assuming they're always present.
    """

    event_bus: EventBus
    settings: Any
    plugin_config: dict[str, Any] = field(default_factory=dict)
    reasoning_engine: "ReasoningEngine | None" = None
    evidence_aggregator: "EvidenceAggregator | None" = None
    strategy_engine: "StrategyEngine | None" = None


class PluginBase(ABC):
    """Base class every plugin inherits from."""

    #: Override in subclasses — used for logging, registry keys, and config lookup.
    name: str = "unnamed-plugin"
    version: str = "0.1.0"
    category: str = "uncategorized"

    def __init__(self, context: PluginContext) -> None:
        self.context = context

    # ---------------------------------------------------------------- contract

    @abstractmethod
    async def initialize(self) -> None:
        """Acquire resources and subscribe to events. Called once at startup."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources cleanly. Called once at shutdown."""

    @abstractmethod
    async def health(self) -> PluginHealth:
        """Report whether this plugin is currently working."""

    @abstractmethod
    def config(self) -> dict[str, Any]:
        """Return this plugin's current configuration values."""

    @abstractmethod
    def permissions(self) -> list[str]:
        """Declare what this plugin needs access to (see :class:`PluginPermission`)."""

    # ---------------------------------------------------------------- convenience

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name=self.name,
            version=self.version,
            category=self.category,
            description=(self.__doc__ or "").strip(),
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r} version={self.version!r}>"
