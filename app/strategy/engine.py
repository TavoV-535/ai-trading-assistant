"""
The Strategy Engine.

Subscribes to ``EvidenceAggregated`` (never raw ``EvidenceProduced`` —
the Evidence Aggregator is its only source of evidence) and re-evaluates
every compiled strategy against the affected symbol's current active
evidence snapshot. Publishes ``StrategyMatched`` the moment a strategy
transitions from not-matched to matched — edge-triggered, the same
"don't spam on every tick a condition continues to hold" philosophy every
indicator plugin in this codebase already follows.

Knows nothing about indicators. Adding a new indicator plugin makes its
evidence titles usable by any strategy's ``required``/``optional`` lists
with zero changes here — this module only ever imports
``app.strategy.compiler``/``app.strategy.models`` and the Event Bus.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.event_bus.bus import EventBus
from app.event_bus.events import EvidenceAggregated, StrategyMatched
from app.logging import get_logger
from app.strategy.compiler import CompiledStrategy, StrategyEvaluation
from app.strategy.loader import load_strategies

log = get_logger(__name__)


class StrategyEngine:
    """Loads compiled strategies and evaluates them against every symbol's
    evidence as it's aggregated."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._strategies: list[CompiledStrategy] = []
        self._matched: dict[tuple[str, str], bool] = {}
        self._event_bus: EventBus | None = None

    def load(self, project_root: Path) -> None:
        """Load every strategy under ``project_root/plugins/strategies``.
        Safe to call again to hot-reload — replaces the compiled set
        entirely (matched-state for symbol/strategy pairs no longer
        present is simply dropped)."""
        self._strategies = load_strategies(project_root / "plugins" / "strategies")
        log.info("strategy_engine_loaded", count=len(self._strategies))

    @property
    def strategies(self) -> list[CompiledStrategy]:
        return list(self._strategies)

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(EvidenceAggregated, self._on_aggregated, name="strategy_engine")
        log.info("strategy_engine_attached", strategies=[s.name for s in self._strategies])

    # ---------------------------------------------------------------- handler

    async def _on_aggregated(self, event: EvidenceAggregated) -> None:
        for evaluation in self.evaluate_all(event.symbol, event.active_evidence):
            key = (event.symbol, evaluation.strategy)
            was_matched = self._matched.get(key, False)
            self._matched[key] = evaluation.matched

            if evaluation.matched and not was_matched and self._event_bus is not None:
                await self._event_bus.publish(
                    StrategyMatched(
                        source="StrategyEngine",
                        strategy=evaluation.strategy,
                        symbol=event.symbol,
                        score=evaluation.score,
                        evidence_count=len(evaluation.contributing_evidence),
                    )
                )
                log.info(
                    "strategy_matched",
                    strategy=evaluation.strategy,
                    symbol=event.symbol,
                    score=evaluation.score,
                    evidence_count=len(evaluation.contributing_evidence),
                )

    # ---------------------------------------------------------------- queries

    def evaluate_all(self, symbol: str, active_evidence: list) -> list[StrategyEvaluation]:
        """Evaluate every loaded strategy against a symbol's active
        evidence, without touching match-state or publishing anything —
        useful for tests, a future ``/analyze`` command, or just
        inspecting "how close is this strategy to matching right now"."""
        return [strategy.evaluate(active_evidence) for strategy in self._strategies]

    def is_matched(self, symbol: str, strategy_name: str) -> bool:
        return self._matched.get((symbol, strategy_name), False)
