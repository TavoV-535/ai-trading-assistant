"""
The Unified Simulation Engine.

The common execution environment for Historical Backtesting, Replay Mode,
Strategy Comparison, Parameter Optimization, and future AI Training —
never a bespoke "Backtesting Engine" with its own execution path. One
``SimulationEngine.run()`` call is, functionally, a self-contained miniature
``app.core.bootstrap.bootstrap()``: it constructs its own isolated
``EventBus`` and a fresh instance of every core engine (Market Context
Engine, Evidence Aggregator, Strategy Engine, Portfolio Intelligence
Layer, Event Prioritization Engine, Reasoning Engine, Decision Timeline),
loads the exact same plugin categories (market data providers, indicators,
optionally intelligence sources) through the exact same
``PluginRegistry``, and then drives historical bars through them one at a
time.

**No simulation-specific shortcuts.** Every event this engine publishes —
``MarketDataUpdated``, and transitively everything indicator plugins,
the aggregator, the strategy engine, the context engine, the portfolio
and prioritization engines produce in reaction — is the exact same event
class live operation publishes. A command plugin (``/analyze`` included)
querying these engines during or after a run cannot tell the difference
between "this state came from a live scanner" and "this state came from a
historical replay" — see ``tests/test_milestone9_pipeline_integration.py``
for the test that proves it by literally running the real
``AnalyzePlugin`` against a simulation's engines mid-run.

**Determinism.** Given identical historical data (the configured market
data provider's own deterministic output — see
``plugins/market_data/replay/plugin.py``) and identical configuration,
two runs produce an identical sequence of events and an identical
Decision Timeline. Three things make this true:

1. A ``SimulatedClock`` (``app/core/clock.py``), injected into the
   Evidence Aggregator, Portfolio Intelligence Layer, and Event
   Prioritization Engine, replaces every wall-clock read those engines
   would otherwise make (freshness/decay, confidence trend, alert
   cooldown/suppression) with the current *simulated* bar's timestamp —
   advanced explicitly, once per bar, never by real elapsed time.
2. Every published event's own ``timestamp`` is set explicitly to the
   simulated bar time, never left to default to ``datetime.now()``.
3. ``EventBus.drain()`` is awaited after every bar's publishes (and after
   every intelligence poll) — the full downstream cascade (indicators →
   aggregator → strategy engine → context/portfolio/prioritization →
   reasoning engine) fully settles, in a fixed, single-threaded order,
   before the next bar is published. Nothing here depends on real
   asyncio task-scheduling races.

A concrete plugin's own cosmetic timestamps (``Evidence.created_at``,
``IndicatorCalculated.timestamp``) are NOT simulated — nothing in this
codebase's decision logic reads them (verified; see ``app/core/clock.py``'s
docstring), so this is a documented, inconsequential scope boundary, not a
determinism gap in anything that actually affects a decision.

**The Reasoning Engine never calls a real AI provider during simulation**
— non-deterministic, costs real API calls, and unnecessary for
reproducible historical analysis. Every simulation run reasons in
``evidence_only`` mode, exactly the same code path live operation already
uses whenever no provider is configured (this sandbox's actual default —
see the Milestone 8 live demo).

**Intelligence plugins** (News/Earnings/Macro) are loaded like any other
plugin, but their own background polling task is cancelled immediately
after ``initialize()`` (each plugin's ``poll_once()`` is deterministic —
see ``app/intelligence/plugin.py`` — but its background loop sleeps on
real wall-clock intervals, which would race against the simulated
timeline). The engine calls ``poll_once()`` directly instead, on a fixed
simulated cadence.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.aggregation.aggregator import EvidenceAggregator
from app.aggregation.models import AggregateSnapshot
from app.context.engine import MarketContextEngine
from app.core.clock import SimulatedClock
from app.event_bus.bus import EventBus
from app.event_bus.events import AlertGenerated, DecisionRecorded, MarketDataUpdated
from app.evidence.schema import FUNDAMENTAL_CATEGORIES
from app.intelligence.plugin import IntelligencePlugin
from app.logging import get_logger
from app.marketdata.service import MarketDataService
from app.plugins.registry import PluginRegistry
from app.portfolio.engine import PortfolioIntelligenceEngine
from app.prioritization.engine import EventPrioritizationEngine
from app.reasoning.engine import ReasoningEngine, ReasoningOutput
from app.simulation.config import SimulationConfig, SimulationResult
from app.strategy.engine import StrategyEngine
from app.timeline.engine import DecisionTimeline

log = get_logger(__name__)

#: simulated_action -> the direction it implies, for outcome resolution.
#: "no_action" deliberately maps to None -- there's no directional claim
#: to grade an outcome against.
_ACTION_DIRECTIONS: dict[str, str] = {
    "watch_bullish": "bullish",
    "watch_bearish": "bearish",
    "watch_neutral": "neutral",
}


@dataclass
class _PendingDecision:
    """A decision already built (full reasoning snapshot captured) but
    not yet published — waiting for ``lookahead_bars`` more simulated
    bars of price data so its outcome can be resolved before it's
    published exactly once, fully formed. Events are immutable in this
    codebase; this is what keeps that true here too."""

    event: DecisionRecorded
    bar_index: int
    symbol: str
    entry_price: float


class SimulationEngine:
    """Runs one deterministic historical simulation per ``run()`` call.
    Stateless between calls — construct once, call ``run()`` as many times
    as needed (each call gets its own fully isolated engines/event bus),
    which is what makes Strategy Comparison and Parameter Optimization
    ("run the same historical window under different configs and compare
    results") already supported by construction, even though this
    milestone doesn't yet ship a comparison UI on top of it."""

    def __init__(self, settings: Any, *, project_root: Path) -> None:
        self._base_settings = settings
        self._project_root = project_root

    async def run(self, config: SimulationConfig) -> SimulationResult:
        symbols = list(dict.fromkeys(config.symbols))  # de-dup, preserve order
        if not symbols:
            raise ValueError("SimulationConfig.symbols must not be empty")

        settings = self._scoped_settings(symbols)
        sim = settings.simulation

        timeframe = config.timeframe or sim.default_timeframe
        bar_count = config.bar_count if config.bar_count is not None else sim.default_bar_count
        pace = config.pace or sim.pace
        decision_interval_bars = max(1, config.decision_interval_bars if config.decision_interval_bars is not None else sim.decision_interval_bars)
        lookahead_bars = max(1, config.lookahead_bars if config.lookahead_bars is not None else sim.lookahead_bars)
        include_intelligence = sim.include_intelligence if config.include_intelligence is None else config.include_intelligence
        intelligence_poll_interval_bars = max(1, sim.intelligence_poll_interval_bars)
        bar_interval = timedelta(seconds=sim.bar_interval_seconds)
        neutral_band_pct = sim.outcome_neutral_band_pct
        run_id = config.correlation_id or uuid4()

        clock = SimulatedClock(start=config.start_time)
        event_bus = EventBus.from_settings(settings)

        context_engine = MarketContextEngine(settings, clock=clock)
        context_engine.attach(event_bus)
        evidence_aggregator = EvidenceAggregator(settings, clock=clock)
        evidence_aggregator.attach(event_bus)
        strategy_engine = StrategyEngine(settings, clock=clock)
        strategy_engine.load(self._project_root)
        strategy_engine.attach(event_bus)
        portfolio_engine = PortfolioIntelligenceEngine(settings, clock=clock)
        portfolio_engine.attach(event_bus)
        prioritization_engine = EventPrioritizationEngine(settings, clock=clock)
        prioritization_engine.attach(event_bus)
        # Deliberately provider=None -- see module docstring: a simulation
        # never calls a real AI provider, for determinism and reproducibility.
        reasoning_engine = ReasoningEngine(settings, provider=None)
        reasoning_engine.attach(event_bus)
        decision_timeline = DecisionTimeline(settings)
        decision_timeline.attach(event_bus)

        alerts: list[AlertGenerated] = []

        async def _capture_alert(event: AlertGenerated) -> None:
            alerts.append(event)

        event_bus.subscribe(AlertGenerated, _capture_alert, name="simulation_alert_capture")

        plugin_registry = PluginRegistry(
            event_bus,
            settings,
            reasoning_engine=reasoning_engine,
            evidence_aggregator=evidence_aggregator,
            strategy_engine=strategy_engine,
            context_engine=context_engine,
            portfolio_engine=portfolio_engine,
        )
        # Phase 1, exactly like app.core.bootstrap: market data providers
        # first, so the Market Data Abstraction Layer can be built from them.
        await plugin_registry.load_all(self._project_root, search_paths=["plugins/market_data"])
        market_data_service = MarketDataService(settings, plugin_registry)
        plugin_registry.set_market_data_service(market_data_service)

        # Phase 2: indicators always; intelligence sources only if asked.
        phase_two_paths = ["plugins/indicators"]
        if include_intelligence:
            phase_two_paths.append("plugins/intelligence")
        await plugin_registry.load_all(self._project_root, search_paths=phase_two_paths)

        intelligence_plugins = [p for p in plugin_registry.plugins.values() if isinstance(p, IntelligencePlugin)]
        for plugin in intelligence_plugins:
            # Cancel each plugin's real background polling task -- poll_once()
            # itself is fully deterministic (see app/intelligence/plugin.py),
            # but its wall-clock-interval background loop is not welcome in a
            # simulated timeline. This engine calls poll_once() directly
            # instead, below, on a fixed simulated cadence.
            await plugin.shutdown()

        log.info(
            "simulation_run_starting",
            run_id=str(run_id),
            symbols=symbols,
            timeframe=timeframe,
            bar_count=bar_count,
            pace=pace,
        )

        pending: list[_PendingDecision] = []
        price_history: dict[str, list[float]] = defaultdict(list)
        decisions_recorded = 0

        for bar_index in range(bar_count):
            sim_time = clock.now() if bar_index == 0 else clock.tick(bar_interval)

            bars = await market_data_service.fetch(symbols, timeframe)
            for symbol in symbols:
                bar = bars.get(symbol)
                if bar is None:
                    continue
                price_history[symbol].append(bar.close)
                await event_bus.publish(
                    MarketDataUpdated(
                        source="SimulationEngine",
                        symbol=symbol,
                        price=bar.close,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=int(bar.volume),
                        timeframe=timeframe,
                        timestamp=sim_time,
                        correlation_id=run_id,
                    )
                )
            await event_bus.drain()

            if include_intelligence and bar_index % intelligence_poll_interval_bars == 0:
                for plugin in intelligence_plugins:
                    await plugin.poll_once()
                await event_bus.drain()

            if bar_index % decision_interval_bars == 0:
                for symbol in symbols:
                    prices = price_history.get(symbol)
                    if not prices:
                        continue
                    decision_event = await self._build_decision(
                        symbol=symbol,
                        sim_time=sim_time,
                        bar_index=bar_index,
                        lookahead_bars=lookahead_bars,
                        run_id=run_id,
                        price=prices[-1],
                        evidence_aggregator=evidence_aggregator,
                        context_engine=context_engine,
                        reasoning_engine=reasoning_engine,
                        portfolio_engine=portfolio_engine,
                    )
                    pending.append(
                        _PendingDecision(event=decision_event, bar_index=bar_index, symbol=symbol, entry_price=prices[-1])
                    )

            still_pending: list[_PendingDecision] = []
            for item in pending:
                if bar_index - item.bar_index >= lookahead_bars:
                    resolved = _resolve_outcome(item, price_history[item.symbol], neutral_band_pct)
                    await event_bus.publish(resolved)
                    decisions_recorded += 1
                else:
                    still_pending.append(item)
            pending = still_pending

            if pace == "realtime":
                await asyncio.sleep(sim.bar_interval_seconds)

        # Flush whatever's still pending at run end, honestly unresolved --
        # never fabricate an outcome from data that doesn't exist yet.
        for item in pending:
            await event_bus.publish(item.event)
            decisions_recorded += 1
        await event_bus.drain()

        log.info(
            "simulation_run_complete",
            run_id=str(run_id),
            bars_processed=bar_count,
            decisions_recorded=decisions_recorded,
            decisions_pending_at_end=len(pending),
            alerts_generated=len(alerts),
        )

        return SimulationResult(
            run_id=run_id,
            bars_processed=bar_count,
            decisions_recorded=decisions_recorded,
            decisions_pending_at_end=len(pending),
            symbols=symbols,
            settings=settings,
            event_bus=event_bus,
            clock=clock,
            context_engine=context_engine,
            evidence_aggregator=evidence_aggregator,
            strategy_engine=strategy_engine,
            reasoning_engine=reasoning_engine,
            portfolio_engine=portfolio_engine,
            prioritization_engine=prioritization_engine,
            decision_timeline=decision_timeline,
            plugin_registry=plugin_registry,
            market_data_service=market_data_service,
            alerts=alerts,
        )

    # ---------------------------------------------------------------- helpers

    def _scoped_settings(self, symbols: list[str]) -> Any:
        """A deep copy of the base settings, scoped to this run: every
        simulated symbol is guaranteed to be on the Portfolio Intelligence
        Layer's watchlist (so its priority score, confidence trend, and
        matched-strategies list are actually tracked and available to the
        Decision Timeline), without ever mutating the live, shared settings
        object other code may be using concurrently."""
        settings = self._base_settings.model_copy(deep=True)
        watchlist = list(settings.portfolio.watchlist)
        for symbol in symbols:
            if symbol not in watchlist:
                watchlist.append(symbol)
        settings.portfolio.watchlist = watchlist
        return settings

    async def _build_decision(
        self,
        *,
        symbol: str,
        sim_time: datetime,
        bar_index: int,
        lookahead_bars: int,
        run_id: UUID,
        price: float,
        evidence_aggregator: EvidenceAggregator,
        context_engine: MarketContextEngine,
        reasoning_engine: ReasoningEngine,
        portfolio_engine: PortfolioIntelligenceEngine,
    ) -> DecisionRecorded:
        """Build one fully-formed (except outcome) decision snapshot,
        using the exact same query surface ``/analyze`` uses — no parallel
        reasoning path. See ``app/event_bus/events.py::DecisionRecorded``."""
        snapshot = evidence_aggregator.snapshot(symbol)
        output = await reasoning_engine.analyze(symbol)
        context = {**context_engine.snapshot(None), **context_engine.snapshot(symbol)}

        technical: list[str] = []
        fundamental: list[str] = []
        for item in snapshot.active_evidence:
            line = f"{item.source}: {item.title} ({item.direction}, {item.confidence:.0f}/100)"
            (fundamental if item.category in FUNDAMENTAL_CATEGORIES else technical).append(line)

        confidence_weights = {f"{w.evidence.source}:{w.evidence.title}": round(w.weight, 4) for w in snapshot.weighted_evidence}

        matched_strategies: list[str] = []
        profile = portfolio_engine.snapshot(symbol)
        if profile is not None:
            matched_strategies = list(profile.matched_strategies)

        action = _infer_action(output, snapshot)

        return DecisionRecorded(
            source="SimulationEngine",
            timestamp=sim_time,
            correlation_id=run_id,
            symbol=symbol,
            market_context=context,
            technical_evidence=technical,
            fundamental_evidence=fundamental,
            confidence_weights=confidence_weights,
            strategy_matches=matched_strategies,
            reasoning_summary=output.market_summary,
            reasoning_source=output.source,
            confidence=output.confidence,
            simulated_action=action,
            price_at_decision=price,
            bar_index=bar_index,
            lookahead_bars=lookahead_bars,
            outcome=None,
            outcome_price_change_pct=None,
            outcome_pending=True,
        )


def _infer_action(output: ReasoningOutput, snapshot: AggregateSnapshot) -> str:
    """Deliberately independent of ``ReasoningEngine``'s own internal
    evidence-only lean computation (which isn't exposed as a structured
    field) — a small, self-contained, independently-tested rule: weighted
    bullish mass vs. weighted bearish mass, falling back to raw
    bullish/bearish counts if no weighted evidence exists yet. Always
    phrased as a "watch" hypothesis label, never "buy"/"sell" — this
    platform is explicitly not a signal-selling bot."""
    if output.source == "insufficient_evidence":
        return "no_action"

    weighted_mass: dict[str, float] = defaultdict(float)
    for w in snapshot.weighted_evidence:
        weighted_mass[w.evidence.direction] += w.weight
    bullish = weighted_mass.get("bullish", 0.0)
    bearish = weighted_mass.get("bearish", 0.0)
    if not weighted_mass:
        bullish, bearish = float(snapshot.bullish_count), float(snapshot.bearish_count)

    if bullish > bearish:
        return "watch_bullish"
    if bearish > bullish:
        return "watch_bearish"
    return "watch_neutral"


def _resolve_outcome(pending: _PendingDecision, prices: list[float], neutral_band_pct: float) -> DecisionRecorded:
    """Compare the decision's implied direction against real subsequent
    price action over the lookahead window — an honest "was the
    directional read right," never a P&L/backtesting claim (that's a
    distinct, future capability). ``no_action`` decisions have no
    direction to grade, so they resolve to ``outcome=None`` immediately
    (not "pending" — there's nothing left to wait for)."""
    event = pending.event
    direction = _ACTION_DIRECTIONS.get(event.simulated_action)
    if direction is None or not pending.entry_price:
        return event.model_copy(update={"outcome": None, "outcome_price_change_pct": None, "outcome_pending": False})

    exit_price = prices[-1]
    pct_change = (exit_price - pending.entry_price) / pending.entry_price * 100

    if abs(pct_change) <= neutral_band_pct:
        outcome = "neutral"
    elif direction == "bullish":
        outcome = "correct" if pct_change > 0 else "incorrect"
    elif direction == "bearish":
        outcome = "correct" if pct_change < 0 else "incorrect"
    else:  # direction == "neutral"
        outcome = "neutral"

    return event.model_copy(update={"outcome": outcome, "outcome_price_change_pct": round(pct_change, 4), "outcome_pending": False})
