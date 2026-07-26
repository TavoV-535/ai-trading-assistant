"""
The Reflection Engine.

Per PROJECT.md's Milestone 10 spec: after every completed trade or
completed simulation, automatically generate a structured post-trade
analysis. This platform has no real trade execution system yet (that's a
future, honestly-scoped capability — see ``app/journal/models.py``'s
``broker_execution`` placeholder), so the concrete unit of "a completed
trade" today is a resolved ``DecisionRecorded`` — the Simulation Engine's
hypothesis-labeled, outcome-graded decision record (Milestone 9). Every
time one resolves (``outcome_pending`` flips to ``False``), whether that's
a real directional correct/incorrect/neutral verdict or an honest "no
evidence existed" for a ``no_action`` decision, this engine generates
exactly one ``ReflectionGenerated`` event.

Not a plugin — a core service, the same tier as the Decision Timeline or
Portfolio Intelligence Layer. Reads nothing except the ``DecisionRecorded``
event that triggered it, plus its own cached
``SymbolProfileUpdated.confidence_trend`` (the same cache-only pattern the
Event Prioritization Engine already established — see
``app/prioritization/engine.py``). Never reaches into the Evidence
Aggregator, Reasoning Engine, or Portfolio Intelligence Layer directly —
"no subsystem communicates directly with another," per this milestone's
spec, holds structurally, not just by convention (see
``tests/test_milestone10_pipeline_integration.py``'s import-guardrail
test).

Generation is deterministic and rule-based, never an AI call — the same
default this codebase already establishes for the Reasoning Engine
(``evidence_only`` mode) and the Simulation Engine (``provider=None``
during simulation): free, reproducible, and honest about being a
transparent rule, not a model's opinion.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from app.core.clock import Clock, SystemClock
from app.event_bus.bus import EventBus
from app.event_bus.events import ACTION_DIRECTIONS, DecisionRecorded, ReflectionGenerated, SymbolProfileUpdated
from app.evidence.formatting import parse_evidence_line
from app.logging import get_logger
from app.reflection.models import ReflectionRecord

log = get_logger(__name__)

#: Bounded per symbol so a long-running deployment (or a long simulation)
#: never grows this engine's in-memory footprint without limit -- the
#: durable, unbounded history always remains queryable from the database
#: via EventLogRepository.reflections().
_DEFAULT_HISTORY_MAX_PER_SYMBOL = 200


def _split_evidence(event: DecisionRecorded) -> tuple[list[str], list[str]]:
    """Splits a decision's evidence lines into "supporting" (direction
    agrees with ``simulated_action``'s implied direction) and
    "contradictory" (direction disagrees) — the case *for* and the case
    *against* the decision that was actually made, both made visible.
    Neutral-direction evidence, and any line that doesn't parse (see
    ``app/evidence/formatting.py``), is silently omitted from both — it
    doesn't argue for or against a directional hypothesis. A ``no_action``
    decision has no implied direction, so both lists are always empty for
    it — there's nothing to compare evidence against."""
    direction = ACTION_DIRECTIONS.get(event.simulated_action)
    if direction is None:
        return [], []

    supporting: list[str] = []
    contradictory: list[str] = []
    for line in (*event.technical_evidence, *event.fundamental_evidence):
        parts = parse_evidence_line(line)
        if parts is None:
            continue
        if parts.direction == direction:
            supporting.append(line)
        elif parts.direction != "neutral":
            contradictory.append(line)
    return supporting, contradictory


def _build_lessons(event: DecisionRecorded, contradictory: list[str]) -> tuple[str, str]:
    """A short, deterministic, rule-based (lessons_learned,
    potential_improvements) pair. Never a hindsight trading directive
    ("should have bought") — always framed as an observation about the
    evidence and outcome, or a hypothesis worth considering, matching this
    platform's explicit non-goal of being a signal-selling bot."""
    symbol = event.symbol

    if event.simulated_action == "no_action":
        return (
            f"No hypothesis was formed for {symbol} — insufficient evidence existed at this time.",
            "Revisit once more evidence sources are active or once enough price history accumulates.",
        )

    direction_word = ACTION_DIRECTIONS.get(event.simulated_action, "neutral")

    if event.outcome is None:
        return (
            f"A {direction_word} hypothesis was formed for {symbol}, but its outcome could not be "
            "graded (no entry price was recorded at decision time).",
            "Ensure price data is available at decision time so future outcomes can be resolved.",
        )

    if event.outcome == "correct":
        lessons = f"The {direction_word} hypothesis for {symbol} aligned with subsequent price action."
        if contradictory:
            lessons += (
                f" This held despite {len(contradictory)} contradictory signal(s) present at decision "
                "time, suggesting the supporting evidence was correctly weighted more heavily."
            )
            improvements = "No correction indicated — continue monitoring for confirmation depth."
        else:
            improvements = "No contradictory evidence existed at decision time — continue monitoring for confirmation depth."
        return lessons, improvements

    if event.outcome == "incorrect":
        lessons = f"The {direction_word} hypothesis for {symbol} did not hold against subsequent price action."
        if contradictory:
            lessons += (
                f" {len(contradictory)} contradictory signal(s) were present at decision time and may "
                "have been under-weighted."
            )
            improvements = (
                "Review the Confidence Weighting Framework's treatment of the contradicting source(s) "
                "for this market regime."
            )
        else:
            improvements = (
                "No contradictory evidence existed at decision time — the miss may reflect a genuine "
                "regime shift rather than a weighting issue."
            )
        return lessons, improvements

    # "neutral"
    lessons = f"Price action for {symbol} stayed within the neutral band — the {direction_word} hypothesis was inconclusive."
    improvements = "A tighter or wider neutral band may better classify genuinely flat outcomes — review simulation.outcome_neutral_band_pct."
    return lessons, improvements


class ReflectionEngine:
    """Generates one structured ``ReflectionGenerated`` event per resolved
    ``DecisionRecorded``. Attach once at bootstrap (or once per Simulation
    Engine run — see ``app/simulation/engine.py``); every consumer
    (the Trading Journal, a future AI Coach, Performance Analytics, a
    future Dashboard) subscribes to ``ReflectionGenerated`` independently,
    never through this engine directly."""

    def __init__(self, settings: Any, *, clock: Clock | None = None) -> None:
        section = getattr(settings, "reflection", None)
        self._enabled = bool(getattr(section, "enabled", True))
        max_per_symbol = int(getattr(section, "history_max_per_symbol", _DEFAULT_HISTORY_MAX_PER_SYMBOL))
        self._max_per_symbol = max_per_symbol
        #: Defaults to the real wall clock -- the Simulation Engine injects
        #: a SimulatedClock so a reflection's own timestamp stays
        #: consistent with the simulated timeline (see app/core/clock.py;
        #: this is exactly the determinism gap Milestone 9 found and fixed
        #: for AlertGenerated and friends — not repeating it here).
        self._clock: Clock = clock or SystemClock()
        self._confidence_trend: dict[str, str] = defaultdict(lambda: "unknown")
        self._by_symbol: dict[str, "deque[ReflectionRecord]"] = defaultdict(lambda: deque(maxlen=self._max_per_symbol))
        self._total_generated = 0
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="reflection_engine_decisions")
        event_bus.subscribe(SymbolProfileUpdated, self._on_profile_updated, name="reflection_engine_profile")
        log.info("reflection_engine_attached", enabled=self._enabled, history_max_per_symbol=self._max_per_symbol)

    # ---------------------------------------------------------------- profile tracking

    async def _on_profile_updated(self, event: SymbolProfileUpdated) -> None:
        self._confidence_trend[event.symbol] = event.confidence_trend

    # ---------------------------------------------------------------- reflection generation

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        if not self._enabled or event.outcome_pending:
            return

        supporting, contradictory = _split_evidence(event)
        lessons_learned, potential_improvements = _build_lessons(event, contradictory)

        reflection = ReflectionGenerated(
            source="ReflectionEngine",
            timestamp=self._clock.now(),
            correlation_id=event.correlation_id,
            symbol=event.symbol,
            decision_event_id=event.event_id,
            reasoning=event.reasoning_summary,
            supporting_evidence=supporting,
            contradictory_evidence=contradictory,
            market_context=dict(event.market_context),
            confidence=event.confidence,
            confidence_evolution=self._confidence_trend[event.symbol],
            simulated_action=event.simulated_action,
            outcome=event.outcome,
            outcome_price_change_pct=event.outcome_price_change_pct,
            lessons_learned=lessons_learned,
            potential_improvements=potential_improvements,
        )

        record = ReflectionRecord.from_event(reflection)
        self._by_symbol[event.symbol].append(record)
        self._total_generated += 1
        log.debug(
            "reflection_generated",
            symbol=event.symbol,
            decision_event_id=str(event.event_id),
            outcome=event.outcome,
            supporting=len(supporting),
            contradictory=len(contradictory),
        )

        if self._event_bus is not None:
            await self._event_bus.publish(reflection)

    # ---------------------------------------------------------------- queries

    @property
    def total_generated(self) -> int:
        return self._total_generated

    def for_symbol(self, symbol: str, *, limit: int | None = None) -> list[ReflectionRecord]:
        """Every reflection generated for ``symbol``, oldest first."""
        records = list(self._by_symbol.get(symbol, []))
        if limit is not None and limit < len(records):
            return records[-limit:]
        return records

    def all(self, *, limit: int | None = None) -> list[ReflectionRecord]:
        """Every reflection generated across every symbol, oldest first
        overall (stable-sorted by timestamp)."""
        records = sorted((r for bucket in self._by_symbol.values() for r in bucket), key=lambda r: r.timestamp)
        if limit is not None and limit < len(records):
            return records[-limit:]
        return records

    def symbols(self) -> list[str]:
        return sorted(self._by_symbol.keys())
