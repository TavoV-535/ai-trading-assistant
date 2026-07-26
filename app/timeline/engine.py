"""
The Decision Timeline.

Not a plugin — a core service, the same tier as the Evidence Aggregator or
Portfolio Intelligence Layer. Subscribes to ``DecisionRecorded`` (published
today only by the Simulation Engine — see ``app/simulation/``) and builds a
bounded, queryable, per-symbol history of every recorded decision: the
complete reasoning snapshot (market context, technical + fundamental
evidence, confidence weights, matched strategies, reasoning summary,
simulated action) plus its retroactively-resolved outcome.

This is the "canonical historical record" PROJECT.md's Milestone 9 spec
asks future Replay Mode, Journaling, AI Coach, Performance Analytics, and
Explainability features to consume — they read it the same way any command
plugin reads any other core engine's query surface (``for_symbol()``,
``all()``), never by re-deriving reasoning themselves.

Durable persistence needs no new database table: every event on the bus,
``DecisionRecorded`` included, is already persisted verbatim by
``attach_event_logger`` (``app/db/event_logger.py``) via the existing
Repository pattern. ``EventLogRepository.decision_records()``
(``app/db/repository.py``) reconstructs ``DecisionRecord`` objects
straight from those durable rows. This in-memory engine is the fast,
process-local view; the database is the durable one — the same split
every other core engine in this codebase already has between its
in-memory state and the event log.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from app.event_bus.bus import EventBus
from app.event_bus.events import CoachingEvent, DecisionRecorded, RiskEvent
from app.logging import get_logger
from app.timeline.models import DecisionRecord
from app.timeline.visualization import TimelineEntry, build_symbol_timeline

if TYPE_CHECKING:
    # Type-only: importing these at module level closes a circular import
    # (app.journal / app.reflection both import back into app.timeline at
    # module load time -- see app/timeline/visualization.py's docstring for
    # the full cycle). `from __future__ import annotations` above already
    # makes every annotation in this file a lazily-evaluated string, so the
    # TYPE_CHECKING guard costs nothing at runtime.
    from app.journal.models import JournalNote
    from app.reflection.models import ReflectionRecord

log = get_logger(__name__)

#: Bounded per symbol so a long-running deployment (or a long simulation)
#: never grows this engine's in-memory footprint without limit -- the
#: durable, unbounded history always remains queryable from the database
#: via EventLogRepository.decision_records().
_DEFAULT_MAX_PER_SYMBOL = 500


class DecisionTimeline:
    """Maintains a bounded, queryable, per-symbol history of recorded
    decisions. Attach once at bootstrap (or once per Simulation Engine
    run — see ``app/simulation/engine.py``); every consumer queries it via
    ``for_symbol()``/``all()``, the same read-only-query pattern every
    other core engine in this codebase exposes."""

    def __init__(self, settings: Any, *, max_per_symbol: int | None = None) -> None:
        section = getattr(settings, "simulation", None)
        self._max_per_symbol = (
            max_per_symbol if max_per_symbol is not None else int(getattr(section, "timeline_max_per_symbol", _DEFAULT_MAX_PER_SYMBOL))
        )
        self._by_symbol: dict[str, "deque[DecisionRecord]"] = defaultdict(lambda: deque(maxlen=self._max_per_symbol))
        self._total_recorded = 0

    def attach(self, event_bus: EventBus) -> None:
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="decision_timeline")
        log.info("decision_timeline_attached", max_per_symbol=self._max_per_symbol)

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        record = DecisionRecord.from_event(event)
        self._by_symbol[event.symbol].append(record)
        self._total_recorded += 1
        log.debug(
            "decision_recorded",
            symbol=event.symbol,
            simulated_action=event.simulated_action,
            outcome=event.outcome,
            bar_index=event.bar_index,
        )

    # ---------------------------------------------------------------- queries

    @property
    def total_recorded(self) -> int:
        return self._total_recorded

    def for_symbol(self, symbol: str, *, limit: int | None = None) -> list[DecisionRecord]:
        """Every recorded decision for ``symbol``, oldest first. ``limit``
        (if given) returns only the most recent ``limit`` entries."""
        records = list(self._by_symbol.get(symbol, []))
        if limit is not None and limit < len(records):
            return records[-limit:]
        return records

    def all(self, *, limit: int | None = None) -> list[DecisionRecord]:
        """Every recorded decision across every symbol, oldest first
        overall (stable-sorted by timestamp)."""
        records = sorted((r for bucket in self._by_symbol.values() for r in bucket), key=lambda r: r.timestamp)
        if limit is not None and limit < len(records):
            return records[-limit:]
        return records

    def symbols(self) -> list[str]:
        return sorted(self._by_symbol.keys())

    def timeline_for_symbol(
        self,
        symbol: str,
        *,
        reflections: list[ReflectionRecord] = (),
        journal_notes: list[JournalNote] = (),
        risk_events: list[RiskEvent] = (),
        coaching_events: list[CoachingEvent] = (),
        limit: int | None = None,
    ) -> list[TimelineEntry]:
        """The Milestone 12 "Timeline Visualization Data" surface — a
        unified, chronologically ordered view of this symbol's decisions
        plus whatever else the caller already fetched from the
        Reflection Engine / Trading Journal / Capital Protection Engine /
        Learning Engine (all optional, all read-only inputs — this method
        never reaches into those engines itself). See
        ``app/timeline/visualization.py`` for the pure composition
        function this delegates to; no dashboard dependency is
        introduced here or there."""
        return build_symbol_timeline(
            symbol,
            decisions=self.for_symbol(symbol, limit=limit),
            reflections=list(reflections),
            journal_notes=list(journal_notes),
            risk_events=list(risk_events),
            coaching_events=list(coaching_events),
        )
