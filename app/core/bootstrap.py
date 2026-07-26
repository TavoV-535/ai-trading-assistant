"""
Startup and shutdown sequencing for the whole application.

Kept separate from the FastAPI app so it can be exercised directly in tests
(and eventually by the Discord bot entrypoint) without needing an ASGI
server running.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from app.aggregation.aggregator import EvidenceAggregator
from app.analytics.calibration import ConfidenceCalibrationService
from app.analytics.evidence_reliability import EvidenceReliabilityEngine
from app.analytics.service import AnalyticsService
from app.analytics.strategy_analytics import StrategyAnalyticsService
from app.capital_protection.engine import CapitalProtectionEngine
from app.config import get_settings
from app.context.engine import MarketContextEngine
from app.core.state import AppState
from app.db.base import Database
from app.db.event_logger import attach_event_logger
from app.discord.bot import TradingBot
from app.event_bus.bus import EventBus
from app.journal.engine import TradingJournal
from app.knowledge_graph.graph import KnowledgeGraph
from app.knowledge_graph.query import KnowledgeGraphQueryEngine
from app.learning.engine import LearningEngine
from app.logging import configure_logging, get_logger
from app.marketdata.service import MarketDataService
from app.memory.index import MemoryIndex
from app.plugins.registry import PluginRegistry
from app.portfolio.engine import PortfolioIntelligenceEngine
from app.prioritization.engine import EventPrioritizationEngine
from app.reasoning.engine import ReasoningEngine
from app.reasoning.providers.claude_provider import ClaudeProvider
from app.reflection.engine import ReflectionEngine
from app.replay.service import EventReplayService
from app.strategy.engine import StrategyEngine
from app.timeline.engine import DecisionTimeline

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def bootstrap(settings: Any | None = None, *, project_root: Path | None = None) -> AppState:
    """Bring every core system up, in dependency order, and load plugins.

    A plugin failing to initialize never aborts boot — see
    :meth:`~app.plugins.registry.PluginRegistry.load_all`. A missing AI
    provider never aborts boot either — the Reasoning Engine degrades to
    evidence-only mode (see :mod:`app.reasoning.engine`).
    """
    settings = settings or get_settings()
    root = project_root or PROJECT_ROOT

    configure_logging(settings)
    log.info("bootstrap_starting", env=settings.app.env)

    event_bus = EventBus.from_settings(settings)

    database = Database(settings)
    attach_event_logger(event_bus, database)

    # Market Context Engine derives higher-level market-environment labels
    # (Bull/Bear Trend, High/Low Volatility, Gap Day, Trend Exhaustion,
    # Risk-On/Risk-Off, Fed Week, CPI Day, ...) from MarketDataUpdated and
    # intelligence EvidenceProduced events, publishing MarketContextUpdated.
    # Attached before the Evidence Aggregator so the aggregator's
    # confidence-weighting subscription (below) sees context from the
    # first tick onward — subscription *order* doesn't actually matter to
    # either engine, but this keeps bootstrap reading top-to-bottom as the
    # data actually flows. See app/context/engine.py.
    context_engine = MarketContextEngine(settings)
    context_engine.attach(event_bus)

    # Evidence Aggregator sits between every evidence producer (14
    # indicator plugins + the News/Earnings/Macro intelligence plugins
    # today; more External Intelligence Platform sources later) and
    # everything that consumes evidence. It's the single interface both
    # the Strategy Engine and the Reasoning Engine subscribe to — neither
    # subscribes to raw EvidenceProduced directly. It also subscribes to
    # MarketContextUpdated as a Confidence Weighting Framework input (see
    # app/aggregation/weighting.py) — market regime, not raw evidence.
    evidence_aggregator = EvidenceAggregator(settings)
    evidence_aggregator.attach(event_bus)

    strategy_engine = StrategyEngine(settings)
    strategy_engine.load(root)
    strategy_engine.attach(event_bus)

    # Portfolio Intelligence Layer maintains an evolving profile (evidence,
    # external intelligence freshness, market context, confidence trend,
    # matched strategies, historical alert state) per symbol on
    # settings.portfolio.watchlist, ranking them by a transparent
    # priority_score. Not a plugin -- a core service, the same tier as the
    # Evidence Aggregator / Market Context Engine. See app/portfolio/engine.py.
    portfolio_engine = PortfolioIntelligenceEngine(settings)
    portfolio_engine.attach(event_bus)

    # Event Prioritization Engine sits between the Evidence Aggregator (plus
    # the Strategy Engine and Market Context Engine) and user notifications,
    # scoring every candidate development and only publishing AlertGenerated
    # for what actually clears the configured threshold and isn't a
    # duplicate within the cooldown window. Reads settings.portfolio.watchlist
    # directly (not via portfolio_engine) so watchlist membership is correct
    # from t=0 -- see app/prioritization/engine.py's module docstring for why.
    prioritization_engine = EventPrioritizationEngine(settings)
    prioritization_engine.attach(event_bus)

    provider = None
    if settings.reasoning.enabled and settings.has_anthropic_key:
        provider = ClaudeProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.reasoning.model,
        )
        log.info("reasoning_provider_ready", model=settings.reasoning.model)
    else:
        log.warning(
            "reasoning_provider_not_configured",
            detail="ANTHROPIC_API_KEY missing or reasoning.enabled=false — "
            "the Reasoning Engine will produce evidence-only summaries.",
        )

    reasoning_engine = ReasoningEngine(settings, provider=provider)
    reasoning_engine.attach(event_bus)

    # Decision Timeline + Reflection Engine + Trading Journal (Milestone
    # 10, built on top of Milestone 9's DecisionRecorded/Decision Timeline)
    # — attached live so /journal works immediately once any producer
    # (today: only the Simulation Engine — see app/simulation/engine.py)
    # publishes DecisionRecorded. Nothing publishes it from live market
    # data yet (an honest, documented scope boundary carried over from
    # Milestone 9, not a bug) — these three sit idle, gracefully, until a
    # future live/paper-trading decision-recording mechanism exists, the
    # same "wired up, waiting for a producer" pattern several other
    # Action Registry buttons already use.
    decision_timeline = DecisionTimeline(settings)
    decision_timeline.attach(event_bus)
    reflection_engine = ReflectionEngine(settings)
    reflection_engine.attach(event_bus)
    trading_journal = TradingJournal(settings)
    trading_journal.attach(event_bus)

    # Capital Protection Engine (Milestone 11) -- the exact same class
    # SimulationEngine.run() constructs (see app/simulation/engine.py),
    # attached here for live operation. Continuously evaluates capital-
    # preservation risk from whatever DecisionRecorded/MarketDataUpdated
    # events actually flow; never blocks a trade or command, only
    # publishes structured RiskEvents. Idle-but-attached in live mode
    # today for the same honest reason DecisionTimeline/ReflectionEngine/
    # TradingJournal are: nothing yet publishes DecisionRecorded from live
    # market data (see those engines' comments above).
    capital_protection_engine = CapitalProtectionEngine(settings)
    capital_protection_engine.attach(event_bus)

    # Milestone 12 -- the platform's intelligence layer. Every piece here
    # only *observes* history through the Event Bus (or, for the Event
    # Replay API, the durable event log) and *reasons* about it -- never
    # alters historical data, never blocks a trade or a command. Built in
    # dependency order: the three Analytics collaborators first (each
    # independently subscribes to DecisionRecorded/TradeClosed/RiskEvent),
    # then the Knowledge Graph, then the composed read-only facades
    # (AnalyticsService, KnowledgeGraphQueryEngine) that the Learning Engine
    # and future commands (/coach) consume instead of recalculating
    # anything themselves -- "no duplicated calculations" applied the same
    # way it already is inside app/analytics/ and app/knowledge_graph/.
    confidence_calibration = ConfidenceCalibrationService(settings)
    confidence_calibration.attach(event_bus)
    evidence_reliability = EvidenceReliabilityEngine(settings)
    evidence_reliability.attach(event_bus)
    strategy_analytics = StrategyAnalyticsService(settings)
    strategy_analytics.attach(event_bus)
    analytics_service = AnalyticsService(
        strategy_analytics=strategy_analytics,
        evidence_reliability=evidence_reliability,
        confidence_calibration=confidence_calibration,
    )

    knowledge_graph = KnowledgeGraph(settings)
    knowledge_graph.attach(event_bus)
    knowledge_graph_query = KnowledgeGraphQueryEngine(
        knowledge_graph,
        evidence_reliability=evidence_reliability,
        confidence_calibration=confidence_calibration,
    )

    learning_engine = LearningEngine(
        settings,
        strategy_analytics=strategy_analytics,
        evidence_reliability=evidence_reliability,
        confidence_calibration=confidence_calibration,
        knowledge_graph_query=knowledge_graph_query,
    )
    learning_engine.attach(event_bus)

    memory_index = MemoryIndex(settings)
    memory_index.attach(event_bus)

    # Event Replay API reads the durable event log directly (via
    # `database`, already constructed above) rather than any in-memory
    # engine -- it's the one Milestone 12 piece with no simulation-side
    # counterpart, since SimulationEngine.run() never persists to a
    # database (see app/simulation/engine.py's module docstring); replaying
    # a simulated decision is already possible by querying that run's own
    # in-memory DecisionTimeline instead.
    event_replay_service = EventReplayService(database)

    # Command plugins (e.g. /analyze) may need to read the *current*
    # evidence/reasoning state synchronously, not just react to events — see
    # PluginContext's docstring for the scope of this exception.
    plugin_registry = PluginRegistry(
        event_bus,
        settings,
        reasoning_engine=reasoning_engine,
        evidence_aggregator=evidence_aggregator,
        strategy_engine=strategy_engine,
        context_engine=context_engine,
        portfolio_engine=portfolio_engine,
        trading_journal=trading_journal,
        capital_protection_engine=capital_protection_engine,
        knowledge_graph=knowledge_graph,
        knowledge_graph_query=knowledge_graph_query,
        analytics_service=analytics_service,
        learning_engine=learning_engine,
        memory_index=memory_index,
        event_replay_service=event_replay_service,
    )

    # Phase 1: market data provider plugins load first, in isolation. The
    # Market Data Abstraction Layer can't exist until concrete provider
    # instances do, and the Scanner Engine (phase 2) needs the abstraction
    # layer, never a specific provider — see app/marketdata/service.py.
    await plugin_registry.load_all(root, search_paths=["plugins/market_data"])
    market_data_service = MarketDataService(settings, plugin_registry)
    plugin_registry.set_market_data_service(market_data_service)

    # Phase 2: everything else -- indicators, commands, scanners, ...
    remaining_paths = [p for p in settings.plugins.search_paths if p != "plugins/market_data"]
    await plugin_registry.load_all(root, search_paths=remaining_paths)

    discord_bot: TradingBot | None = None
    discord_task: "asyncio.Task[None] | None" = None
    if settings.has_discord_token:
        discord_bot = TradingBot(settings, event_bus, plugin_registry)
        token = settings.discord_bot_token.get_secret_value()
        discord_task = asyncio.create_task(discord_bot.start(token))
        log.info("discord_bot_starting")
    else:
        log.warning(
            "discord_bot_not_configured",
            detail="DISCORD_BOT_TOKEN missing — Discord commands will not be reachable "
            "until it's set. See docs/DISCORD_BOT_SETUP.md.",
        )

    log.info(
        "bootstrap_complete",
        plugins_loaded=len(plugin_registry.plugins),
        plugins_failed=len(plugin_registry.failed),
        strategies_loaded=len(strategy_engine.strategies),
        market_data_providers=[p.provider_name for p in market_data_service.providers],
        discord_enabled=discord_bot is not None,
        portfolio_watchlist=list(portfolio_engine.watchlist),
    )

    return AppState(
        settings=settings,
        event_bus=event_bus,
        database=database,
        plugin_registry=plugin_registry,
        evidence_aggregator=evidence_aggregator,
        strategy_engine=strategy_engine,
        reasoning_engine=reasoning_engine,
        market_data_service=market_data_service,
        context_engine=context_engine,
        portfolio_engine=portfolio_engine,
        prioritization_engine=prioritization_engine,
        decision_timeline=decision_timeline,
        reflection_engine=reflection_engine,
        trading_journal=trading_journal,
        capital_protection_engine=capital_protection_engine,
        knowledge_graph=knowledge_graph,
        knowledge_graph_query=knowledge_graph_query,
        analytics_service=analytics_service,
        learning_engine=learning_engine,
        memory_index=memory_index,
        event_replay_service=event_replay_service,
        project_root=root,
        discord_bot=discord_bot,
        discord_task=discord_task,
    )


async def teardown(state: AppState) -> None:
    """Shut everything down in reverse dependency order."""
    log.info("teardown_starting")
    if state.discord_bot is not None:
        await state.discord_bot.close()
    if state.discord_task is not None:
        state.discord_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.discord_task
    await state.plugin_registry.shutdown_all()
    await state.event_bus.shutdown()
    await state.database.dispose()
    log.info("teardown_complete")
