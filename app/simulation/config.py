"""
Simulation Engine configuration and result shapes.

``SimulationConfig`` is what a caller passes to
``SimulationEngine.run()`` — every field is optional except ``symbols``;
anything left unset falls back to ``settings.simulation.*`` (Configuration
over code — a caller overriding one knob for one run never has to restate
the rest).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.aggregation.aggregator import EvidenceAggregator
from app.analytics.service import AnalyticsService
from app.capital_protection.engine import CapitalProtectionEngine
from app.context.engine import MarketContextEngine
from app.core.clock import SimulatedClock
from app.event_bus.bus import EventBus
from app.event_bus.events import AlertGenerated
from app.journal.engine import TradingJournal
from app.knowledge_graph.graph import KnowledgeGraph
from app.knowledge_graph.query import KnowledgeGraphQueryEngine
from app.learning.engine import LearningEngine
from app.marketdata.service import MarketDataService
from app.memory.index import MemoryIndex
from app.plugins.registry import PluginRegistry
from app.portfolio.engine import PortfolioIntelligenceEngine
from app.prioritization.engine import EventPrioritizationEngine
from app.reasoning.engine import ReasoningEngine
from app.reflection.engine import ReflectionEngine
from app.strategy.engine import StrategyEngine
from app.timeline.engine import DecisionTimeline


@dataclass
class SimulationConfig:
    """One simulation run's parameters. ``symbols`` is the only required
    field; every other field defaults to ``settings.simulation.*`` when
    left ``None``, so a caller only ever states what it wants to override.
    """

    symbols: list[str]
    timeframe: str | None = None
    bar_count: int | None = None
    #: The simulated start time. Defaults to a fixed epoch
    #: (``SimulatedClock``'s own default) when left unset — deliberately
    #: NOT ``datetime.now()``, so two runs with identical config produce
    #: an identical timeline even if kicked off on different real days.
    start_time: datetime | None = None
    pace: Literal["instant", "realtime"] | None = None
    decision_interval_bars: int | None = None
    lookahead_bars: int | None = None
    include_intelligence: bool | None = None
    #: Tags every event this run publishes (including every
    #: ``DecisionRecorded``) so they're all traceable back to one run, the
    #: same purpose ``correlation_id`` already serves everywhere else in
    #: this codebase. Auto-generated if not supplied.
    correlation_id: UUID | None = None


@dataclass
class SimulationResult:
    """Everything a caller might want after a run completes: summary
    counts plus every core engine instance the run constructed, wired to
    the run's own isolated ``EventBus`` — a caller can keep querying them
    afterward (``/analyze`` included — see
    ``tests/test_milestone9_pipeline_integration.py``) exactly the way
    live code queries the ones ``bootstrap()`` constructs."""

    run_id: UUID
    bars_processed: int
    decisions_recorded: int
    decisions_pending_at_end: int
    symbols: list[str]
    settings: Any
    event_bus: EventBus
    clock: SimulatedClock
    context_engine: MarketContextEngine
    evidence_aggregator: EvidenceAggregator
    strategy_engine: StrategyEngine
    reasoning_engine: ReasoningEngine
    portfolio_engine: PortfolioIntelligenceEngine
    prioritization_engine: EventPrioritizationEngine
    decision_timeline: DecisionTimeline
    reflection_engine: ReflectionEngine
    trading_journal: TradingJournal
    capital_protection_engine: CapitalProtectionEngine
    knowledge_graph: KnowledgeGraph
    knowledge_graph_query: KnowledgeGraphQueryEngine
    analytics_service: AnalyticsService
    learning_engine: LearningEngine
    memory_index: MemoryIndex
    plugin_registry: PluginRegistry
    market_data_service: MarketDataService
    alerts: list[AlertGenerated] = field(default_factory=list)
