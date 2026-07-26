"""
The Trading Journal.

Per PROJECT.md's Milestone 10 spec: the platform's long-term knowledge
base, built on top of the Decision Timeline (Milestone 9) — combining
Decision Timeline records, strategy matches, technical evidence,
fundamental evidence, market context, confidence evolution, trade
outcomes, user notes, screenshots (placeholder support), and future broker
execution data. **The journal enriches existing timeline records rather
than duplicating them** — see :class:`~app.journal.models.JournalEntry`'s
docstring for exactly what that means structurally.

Not a plugin — a core service, the same tier as the Decision Timeline,
Portfolio Intelligence Layer, or Reflection Engine. Builds its own
independent view purely from events, exactly like every other core engine
in this codebase — it never holds a live reference to the Decision
Timeline or the Reflection Engine (see those modules) to query them
directly; "no subsystem communicates directly with another" holds
structurally here too (see
``tests/test_milestone10_pipeline_integration.py``'s import-guardrail
test):

- ``DecisionRecorded`` — creates a new :class:`JournalEntry` wrapping a
  freshly-built :class:`~app.timeline.models.DecisionRecord`, exactly the
  same reconstruction :class:`~app.timeline.engine.DecisionTimeline` does
  independently from the same event.
- ``ReflectionGenerated`` — attaches a
  :class:`~app.reflection.models.ReflectionRecord` to the matching entry,
  found by ``decision_event_id``.
- ``JournalCreated`` — appends a user note and/or a screenshot placeholder
  to the matching entry (or, if ``decision_event_id`` is omitted — a
  valid, honest "general note about this symbol" — to a separate
  per-symbol general-notes bucket).

Durable persistence needs no new database table: every event on the bus —
``DecisionRecorded``, ``ReflectionGenerated``, and ``JournalCreated``
included — is already persisted verbatim by the existing bus-wide
``attach_event_logger`` subscriber via the Repository pattern. This
in-memory engine is the fast, process-local view; the database
(``EventLogRepository.decision_records()`` / ``.reflections()`` /
``.journal_notes()``) is the durable, unbounded one — the same split every
other core engine in this codebase already has.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from app.core.clock import Clock, SystemClock
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, JournalCreated, ReflectionGenerated
from app.journal.models import JournalEntry, JournalNote
from app.logging import get_logger
from app.reflection.models import ReflectionRecord
from app.timeline.models import DecisionRecord

log = get_logger(__name__)

#: Bounded per symbol so a long-running deployment (or a long simulation)
#: never grows this engine's in-memory footprint without limit -- the
#: durable, unbounded history always remains queryable from the database.
_DEFAULT_MAX_ENTRIES_PER_SYMBOL = 500
_DEFAULT_MAX_NOTES_PER_ENTRY = 50


class TradingJournal:
    """Maintains a bounded, queryable, per-symbol history of enriched
    journal entries. Attach once at bootstrap (or once per Simulation
    Engine run — see ``app/simulation/engine.py``); every consumer
    (``/journal`` included) queries it via ``for_symbol()``/``all()``,
    the same read-only-query pattern every other core engine in this
    codebase exposes."""

    def __init__(self, settings: Any, *, clock: Clock | None = None) -> None:
        section = getattr(settings, "journal", None)
        self._max_entries_per_symbol = int(getattr(section, "max_entries_per_symbol", _DEFAULT_MAX_ENTRIES_PER_SYMBOL))
        self._max_notes_per_entry = int(getattr(section, "max_notes_per_entry", _DEFAULT_MAX_NOTES_PER_ENTRY))
        #: Defaults to the real wall clock -- see app/core/clock.py.
        self._clock: Clock = clock or SystemClock()

        self._entries: dict[UUID, JournalEntry] = {}
        self._by_symbol: dict[str, "deque[UUID]"] = defaultdict(lambda: deque(maxlen=self._max_entries_per_symbol))
        #: Notes (and screenshot placeholders) not tied to any specific
        #: decision -- a valid, honest "general note about this symbol"
        #: (see JournalCreated's docstring), bounded the same way
        #: per-entry notes are.
        self._general_notes: dict[str, "deque[JournalNote]"] = defaultdict(lambda: deque(maxlen=self._max_notes_per_entry))
        self._general_screenshots: dict[str, "deque[str]"] = defaultdict(lambda: deque(maxlen=self._max_notes_per_entry))
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="trading_journal_decisions")
        event_bus.subscribe(ReflectionGenerated, self._on_reflection_generated, name="trading_journal_reflections")
        event_bus.subscribe(JournalCreated, self._on_journal_created, name="trading_journal_notes")
        log.info(
            "trading_journal_attached",
            max_entries_per_symbol=self._max_entries_per_symbol,
            max_notes_per_entry=self._max_notes_per_entry,
        )

    # ---------------------------------------------------------------- event handlers

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        entry = JournalEntry(decision=DecisionRecord.from_event(event))
        self._entries[event.event_id] = entry

        bucket = self._by_symbol[event.symbol]
        evicted = bucket[0] if bucket.maxlen is not None and len(bucket) == bucket.maxlen else None
        bucket.append(event.event_id)
        if evicted is not None:
            self._entries.pop(evicted, None)

        log.debug("journal_entry_created", symbol=event.symbol, decision_event_id=str(event.event_id))

    async def _on_reflection_generated(self, event: ReflectionGenerated) -> None:
        entry = self._entries.get(event.decision_event_id)
        if entry is None:
            # The decision this reflects on was evicted from this
            # in-memory view (or belongs to a different Journal instance
            # entirely, e.g. a separate simulation run) -- honest no-op,
            # not an error. The durable record still has both events.
            log.debug(
                "journal_reflection_unmatched",
                symbol=event.symbol,
                decision_event_id=str(event.decision_event_id),
            )
            return
        entry.reflection = ReflectionRecord.from_event(event)
        log.debug("journal_entry_enriched_with_reflection", symbol=event.symbol, decision_event_id=str(event.decision_event_id))

    async def _on_journal_created(self, event: JournalCreated) -> None:
        symbol = event.symbol
        if symbol is None:
            return
        note = JournalNote(text=event.note or "", author=event.author, added_at=event.timestamp)

        entry = self._entries.get(event.decision_event_id) if event.decision_event_id is not None else None
        if entry is not None:
            if event.note:
                entry.notes.append(note)
                if len(entry.notes) > self._max_notes_per_entry:
                    entry.notes = entry.notes[-self._max_notes_per_entry :]
            if event.screenshot_url:
                entry.screenshots.append(event.screenshot_url)
        else:
            if event.note:
                self._general_notes[symbol].append(note)
            if event.screenshot_url:
                self._general_screenshots[symbol].append(event.screenshot_url)
        log.debug(
            "journal_note_recorded",
            symbol=symbol,
            decision_event_id=str(event.decision_event_id) if event.decision_event_id else None,
            has_screenshot=bool(event.screenshot_url),
        )

    # ---------------------------------------------------------------- write actions

    async def add_note(
        self, *, symbol: str, text: str, decision_event_id: UUID | None = None, author: str | None = None
    ) -> None:
        """Publishes a ``JournalCreated`` event — this engine's own
        ``_on_journal_created`` handler (subscribed like any other
        consumer) is what actually updates in-memory state, the same
        self-consistent event-driven pattern the Decision Timeline uses
        for ``DecisionRecorded``. A no-op (logged) if no event bus is
        attached yet."""
        if self._event_bus is None:
            log.warning("trading_journal_add_note_no_event_bus", symbol=symbol)
            return
        await self._event_bus.publish(
            JournalCreated(
                source="TradingJournal",
                timestamp=self._clock.now(),
                symbol=symbol,
                decision_event_id=decision_event_id,
                note=text,
                author=author,
            )
        )

    async def add_screenshot(
        self, *, symbol: str, screenshot_url: str, decision_event_id: UUID | None = None, author: str | None = None
    ) -> None:
        """Placeholder support only, per the Milestone 10 spec —
        ``screenshot_url`` is stored as-is (a URL or path string); no
        image upload/storage handling exists or is pretended to exist."""
        if self._event_bus is None:
            log.warning("trading_journal_add_screenshot_no_event_bus", symbol=symbol)
            return
        await self._event_bus.publish(
            JournalCreated(
                source="TradingJournal",
                timestamp=self._clock.now(),
                symbol=symbol,
                decision_event_id=decision_event_id,
                author=author,
                screenshot_url=screenshot_url,
            )
        )

    # ---------------------------------------------------------------- queries

    def get(self, decision_event_id: UUID) -> JournalEntry | None:
        return self._entries.get(decision_event_id)

    def for_symbol(self, symbol: str, *, limit: int | None = None) -> list[JournalEntry]:
        """Every journal entry for ``symbol``, oldest first. ``limit``
        (if given) returns only the most recent ``limit`` entries."""
        ids = list(self._by_symbol.get(symbol, []))
        entries = [self._entries[i] for i in ids if i in self._entries]
        if limit is not None and limit < len(entries):
            return entries[-limit:]
        return entries

    def general_notes_for(self, symbol: str) -> list[JournalNote]:
        """Notes added without a specific ``decision_event_id`` — general
        observations about a symbol, not tied to one recorded decision."""
        return list(self._general_notes.get(symbol, []))

    def general_screenshots_for(self, symbol: str) -> list[str]:
        """Screenshot placeholders added without a specific
        ``decision_event_id``."""
        return list(self._general_screenshots.get(symbol, []))

    def all(self, *, limit: int | None = None) -> list[JournalEntry]:
        """Every journal entry across every symbol, oldest first overall
        (stable-sorted by the wrapped decision's timestamp)."""
        entries = sorted(self._entries.values(), key=lambda e: e.decision.timestamp)
        if limit is not None and limit < len(entries):
            return entries[-limit:]
        return entries

    def symbols(self) -> list[str]:
        return sorted(self._by_symbol.keys())

    @property
    def total_entries(self) -> int:
        return len(self._entries)
