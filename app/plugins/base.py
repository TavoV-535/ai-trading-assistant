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

from pydantic import BaseModel, Field

from app.event_bus.bus import EventBus

if TYPE_CHECKING:
    from app.aggregation.aggregator import EvidenceAggregator
    from app.analytics.service import AnalyticsService
    from app.capital_protection.engine import CapitalProtectionEngine
    from app.context.engine import MarketContextEngine
    from app.journal.engine import TradingJournal
    from app.knowledge_graph.graph import KnowledgeGraph
    from app.knowledge_graph.query import KnowledgeGraphQueryEngine
    from app.learning.engine import LearningEngine
    from app.marketdata.service import MarketDataService
    from app.memory.index import MemoryIndex
    from app.plugins.registry import PluginRegistry
    from app.portfolio.engine import PortfolioIntelligenceEngine
    from app.reasoning.engine import ReasoningEngine
    from app.replay.service import EventReplayService
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


class PluginCapabilities(BaseModel):
    """What a plugin advertises it supports — the Milestone 12 "plugin
    capability metadata" architectural recommendation: "plugins can
    advertise supported evidence types, market types, symbols, and
    timeframes without modifying core application code."

    Purely declarative and purely optional. Nothing in the platform
    enforces or filters against these fields automatically — a future
    scanner, router, or command can read them (e.g. "only run this
    plugin for crypto symbols," "only show plugins that produce News
    evidence") without ever importing that plugin's module or special-
    casing its name in core code. That's the point: capability-aware
    behavior lives in whatever consumes ``capabilities()``, never in
    ``PluginBase`` or the registry itself.

    An empty list on any field means "unspecified," not "supports
    nothing" — every plugin written before this milestone doesn't
    override :meth:`PluginBase.capabilities`, so it reports every field
    empty and :meth:`supports` treats that as "no declared restriction,"
    matching everything. A plugin only narrows its surface by actually
    populating a field.
    """

    evidence_types: list[str] = Field(
        default_factory=list,
        description="Evidence categories this plugin produces or consumes, e.g. 'Trend', 'News' (see app.evidence.schema.EvidenceCategory). Empty = unspecified.",
    )
    market_types: list[str] = Field(
        default_factory=list,
        description="Market types this plugin supports, e.g. 'equity', 'crypto', 'forex'. Empty = unspecified.",
    )
    symbols: list[str] = Field(
        default_factory=list,
        description="Specific symbols this plugin is scoped to, if any. Empty = unspecified (assume every symbol).",
    )
    timeframes: list[str] = Field(
        default_factory=list,
        description="Timeframes/intervals this plugin supports, e.g. '1m', '5m', '1d'. Empty = unspecified (assume every timeframe).",
    )

    def supports(
        self,
        *,
        evidence_type: str | None = None,
        market_type: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> bool:
        """True if this plugin's declared capabilities are compatible with
        every criterion given (unspecified criteria and unspecified,
        empty capability lists both count as a match — see class
        docstring)."""
        checks = (
            (evidence_type, self.evidence_types),
            (market_type, self.market_types),
            (symbol, self.symbols),
            (timeframe, self.timeframes),
        )
        return all(wanted is None or not declared or wanted in declared for wanted, declared in checks)


@dataclass
class PluginContext:
    """Everything a plugin is handed at construction time.

    Plugins reach the rest of the system only through this object — never by
    importing core modules directly. This is what keeps a plugin from
    needing to know about anything outside its own folder.

    ``reasoning_engine``, ``evidence_aggregator``, ``strategy_engine``,
    ``market_data_service``, ``plugin_registry``, ``context_engine``,
    ``portfolio_engine``, and ``trading_journal`` are a deliberate, narrow
    exception to "plugins only talk through the Event Bus." They exist so
    a plugin can answer an on-demand, synchronous, read-only query instead
    of only reacting to events — e.g. ``/analyze NVDA`` needs whatever the
    *current* evidence snapshot and reasoning output are right now, not
    whatever the next event happens to publish; a scanner plugin needs the
    *current* bar from the Market Data Abstraction Layer on every tick,
    not an event to react to (it's the thing that starts the event
    chain); ``/scan``'s status report needs to see what's currently
    loaded; ``/analyze`` also needs the Market Context Engine's *current*
    labels for a symbol (and market-wide) to show alongside its evidence,
    and a symbol's *current* Portfolio Intelligence Layer profile
    (priority score, confidence trend, matched strategies) —
    ``/watchlist`` needs the *current* ranked watchlist on demand, not
    just whenever the next ``SymbolProfileUpdated`` happens to fire; and
    (Milestone 10) ``/journal`` needs the Trading Journal's *current*
    enriched entries for a symbol on demand, and (Milestone 11) ``/risk``
    needs the Capital Protection Engine's *current* status snapshot on
    demand. A plugin may read from these (``.snapshot()``, ``.analyze()``,
    ``.matched_strategies_for()``, ``.fetch()``, ``.plugins``,
    ``.ranked_watchlist()``, ``.for_symbol()``, ``.status()``, etc.) but
    must never use them to mutate state directly, publish on another
    system's behalf, or reach into a specific indicator plugin's internals
    — evidence and events remain the only way to make something happen
    (``trading_journal.add_note()`` and
    ``capital_protection_engine.set_active_profile()`` are the two
    write-shaped exceptions; the former only ever works by publishing a
    ``JournalCreated`` event the Journal then reacts to itself, exactly
    like any other subscriber — see ``app/journal/engine.py`` — and the
    latter only ever switches *which already-configured Risk Profile* is
    active, never blocks anything or edits a limit in code — see
    ``app/capital_protection/profiles.py``). They default to ``None``
    (most unit tests, and any future refactor, may not supply them), so
    any plugin reading them must handle ``None`` gracefully instead of
    assuming they're always present.

    (Milestone 12) ``knowledge_graph``, ``knowledge_graph_query``,
    ``analytics_service``, ``learning_engine``, ``memory_index``, and
    ``event_replay_service`` extend this same narrow exception: ``/coach``
    needs the Learning Engine's *current* coaching history and the
    Knowledge Graph Query Layer's *current* explainable answers on demand,
    not just whenever the next ``CoachingEvent`` happens to fire, exactly
    the same shape of need ``/watchlist``/``/journal``/``/risk`` already
    have for their own engines. All five are read-only query surfaces —
    ``knowledge_graph_query.best_strategy_for_context()``,
    ``analytics_service.strategy_stats()``,
    ``learning_engine.review()``/``recent_coaching_events()``,
    ``memory_index.retrieve()``, ``event_replay_service.replay_decision()``
    — never a way to mutate history. The Learning Engine's own "never
    alter historical data, only observe, reason, and publish new events"
    rule from the Milestone 12 spec applies just as much here as it does
    inside ``app/learning/engine.py`` itself.
    """

    event_bus: EventBus
    settings: Any
    plugin_config: dict[str, Any] = field(default_factory=dict)
    reasoning_engine: "ReasoningEngine | None" = None
    evidence_aggregator: "EvidenceAggregator | None" = None
    strategy_engine: "StrategyEngine | None" = None
    market_data_service: "MarketDataService | None" = None
    plugin_registry: "PluginRegistry | None" = None
    context_engine: "MarketContextEngine | None" = None
    portfolio_engine: "PortfolioIntelligenceEngine | None" = None
    trading_journal: "TradingJournal | None" = None
    capital_protection_engine: "CapitalProtectionEngine | None" = None
    knowledge_graph: "KnowledgeGraph | None" = None
    knowledge_graph_query: "KnowledgeGraphQueryEngine | None" = None
    analytics_service: "AnalyticsService | None" = None
    learning_engine: "LearningEngine | None" = None
    memory_index: "MemoryIndex | None" = None
    event_replay_service: "EventReplayService | None" = None


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

    def capabilities(self) -> PluginCapabilities:
        """Declares this plugin's supported evidence types, market types,
        symbols, and timeframes (see :class:`PluginCapabilities`).
        Deliberately concrete, not abstract: the default (everything
        unspecified) means every plugin written before this milestone
        keeps working identically without touching a single existing
        plugin file — "without modifying core application code" applies
        just as much to not modifying every existing plugin. A plugin
        that wants to advertise real capabilities overrides this."""
        return PluginCapabilities()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r} version={self.version!r}>"
