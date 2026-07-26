"""
The Memory Index.

Per PROJECT.md's Milestone 12 spec: "Introduce a Memory Index layer that
allows future AI providers to retrieve only relevant historical
information rather than scanning the full database. The Memory Index
should remain provider-agnostic and local-first. It should support
semantic retrieval later without changing the rest of the architecture."

Not a plugin — a core service, the same tier as the Decision Timeline.
Indexes short, human-readable text pulled from other engines' already-
published events (``ReflectionGenerated``'s lessons/improvements,
``JournalCreated``'s notes, ``CoachingEvent``'s summaries,
``DecisionRecorded``'s reasoning summary) — never raw database rows, and
never modifies any of it.

:meth:`retrieve` today scores candidates by simple case-insensitive
keyword overlap — deliberately not embeddings or any ML — but the
signature (``query: str, top_k: int, ...`` -> ranked
:class:`MemoryEntry` list) is the same shape a future embedding-based
implementation would use. Swapping the scoring function later (real
semantic similarity) means changing ``_score`` in this one module, never
any caller.
"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.event_bus.bus import EventBus
from app.event_bus.events import CoachingEvent, DecisionRecorded, JournalCreated, ReflectionGenerated
from app.logging import get_logger

log = get_logger(__name__)

_WORD_RE = re.compile(r"[a-z0-9']+")
_DEFAULT_MAX_ENTRIES = 5000


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


class MemoryEntry(BaseModel):
    entry_id: str
    text: str
    symbol: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryIndex:
    """Maintains a bounded, queryable index of short historical text
    snippets. Attach once at bootstrap (or once per Simulation Engine
    run); every consumer (a future AI provider, ``/coach``) reads it via
    :meth:`retrieve`, the same read-only-query pattern every other core
    engine in this codebase exposes."""

    def __init__(self, settings: Any, *, max_entries: int | None = None) -> None:
        section = getattr(settings, "memory_index", None)
        self._enabled = bool(getattr(section, "enabled", True))
        self._max_entries = max_entries if max_entries is not None else int(getattr(section, "max_entries", _DEFAULT_MAX_ENTRIES))
        self._entries: "deque[MemoryEntry]" = deque(maxlen=self._max_entries)
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(ReflectionGenerated, self._on_reflection_generated, name="memory_index_reflections")
        event_bus.subscribe(JournalCreated, self._on_journal_created, name="memory_index_journal")
        event_bus.subscribe(CoachingEvent, self._on_coaching_event, name="memory_index_coaching")
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="memory_index_decisions")
        log.info("memory_index_attached", enabled=self._enabled, max_entries=self._max_entries)

    def _index(self, entry: MemoryEntry) -> None:
        if not self._enabled or not entry.text.strip():
            return
        self._entries.append(entry)

    async def _on_reflection_generated(self, event: ReflectionGenerated) -> None:
        text = " ".join(filter(None, [event.reasoning, event.lessons_learned, event.potential_improvements]))
        self._index(
            MemoryEntry(
                entry_id=f"reflection:{event.event_id}", text=text, symbol=event.symbol, timestamp=event.timestamp,
                tags=["reflection", event.outcome or "pending"],
            )
        )

    async def _on_journal_created(self, event: JournalCreated) -> None:
        if not event.note:
            return
        self._index(
            MemoryEntry(
                entry_id=f"journal:{event.event_id}", text=event.note, symbol=event.symbol, timestamp=event.timestamp,
                tags=["journal_note"], metadata={"author": event.author},
            )
        )

    async def _on_coaching_event(self, event: CoachingEvent) -> None:
        text = " ".join(filter(None, [event.title, event.summary, *event.suggested_improvements]))
        self._index(
            MemoryEntry(
                entry_id=f"coaching:{event.event_id}", text=text, symbol=event.symbol, timestamp=event.timestamp,
                tags=["coaching", event.pattern_type],
            )
        )

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        if event.outcome_pending or not event.reasoning_summary.strip():
            return
        self._index(
            MemoryEntry(
                entry_id=f"decision:{event.event_id}", text=event.reasoning_summary, symbol=event.symbol, timestamp=event.timestamp,
                tags=["decision", event.outcome or "unknown"],
            )
        )

    # ---------------------------------------------------------------- queries

    def retrieve(self, query: str, *, top_k: int = 5, symbol: str | None = None, tags: list[str] | None = None) -> list[MemoryEntry]:
        """Ranked, most-relevant-first. Deterministic keyword-overlap
        scoring today (see this module's docstring) — a documented
        placeholder for future semantic retrieval behind the same
        signature."""
        query_words = _words(query)
        if not query_words:
            return []
        candidates = [
            e for e in self._entries
            if (symbol is None or e.symbol == symbol) and (tags is None or any(t in e.tags for t in tags))
        ]
        scored = [(len(query_words & _words(e.text)), e) for e in candidates]
        scored = [(score, e) for score, e in scored if score > 0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def all(self, *, limit: int | None = None) -> list[MemoryEntry]:
        entries = list(self._entries)
        return entries[-limit:] if limit is not None and limit < len(entries) else entries

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy" if self._enabled else "degraded", "enabled": self._enabled, "entries": len(self._entries)}

    def diagnostics(self) -> dict[str, Any]:
        by_tag: dict[str, int] = {}
        for e in self._entries:
            for tag in e.tags:
                by_tag[tag] = by_tag.get(tag, 0) + 1
        return {"enabled": self._enabled, "entries": len(self._entries), "max_entries": self._max_entries, "entries_by_tag": by_tag}

    def statistics(self) -> dict[str, Any]:
        return {"entries": len(self._entries), "generated_at": datetime.now(timezone.utc).isoformat()}
