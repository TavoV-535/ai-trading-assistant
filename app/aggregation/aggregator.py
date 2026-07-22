"""
The Evidence Aggregator.

Sits between every evidence producer (today: 14 indicator plugins; later:
news, earnings, macro, options flow, scanners, ...) and everything that
consumes evidence (the Strategy Engine, the Reasoning Engine). It is the
single interface both of those subscribe to — neither one subscribes to
raw ``EvidenceProduced`` directly.

The aggregator's job is explicitly **not** to suppress or discard market
information — every ``EvidenceProduced`` event it ever receives is kept in
its bounded per-symbol history (``history()``). What it adds on top:

- **Deduplication** — repeated confirmations of the exact same finding
  (same source + title) collapse to one representative in the "active"
  view, while the repeat count is preserved as enrichment metadata
  (``occurrence_count``) rather than being thrown away.
- **Freshness / decay** — each piece of evidence has a freshness that
  decays linearly to zero over ``aggregation.freshness_window_seconds``.
  Only fresh evidence appears in the active snapshot the Strategy Engine
  and Reasoning Engine reason over; stale evidence ages out automatically
  instead of accumulating forever.
- **Conflict detection** — if the currently-fresh evidence for a symbol
  contains both bullish and bearish directions, the snapshot is flagged
  ``has_conflict=True`` rather than silently averaging them away.

Every incoming ``EvidenceProduced`` results in exactly one
``EvidenceAggregated`` event being published, carrying the original
evidence, its enrichment metadata, and the resulting deduped/fresh
snapshot for that symbol.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.aggregation.models import AggregateSnapshot, EnrichmentInfo
from app.event_bus.bus import EventBus
from app.event_bus.events import EvidenceAggregated, EvidenceProduced
from app.evidence.schema import Evidence
from app.logging import get_logger

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Record:
    """One historical evidence occurrence, as retained by the aggregator."""

    evidence: Evidence
    received_at: datetime
    group_key: str


class EvidenceAggregator:
    """Normalizes, enriches, and organizes evidence for one process. Attach
    it to the Event Bus once at bootstrap; every downstream evidence
    consumer subscribes to its ``EvidenceAggregated`` output."""

    def __init__(
        self,
        settings: Any,
        *,
        freshness_window_seconds: float | None = None,
        max_history_per_symbol: int | None = None,
    ) -> None:
        self._freshness_window = (
            freshness_window_seconds
            if freshness_window_seconds is not None
            else settings.aggregation.freshness_window_seconds
        )
        self._max_history = (
            max_history_per_symbol if max_history_per_symbol is not None else settings.aggregation.max_history_per_symbol
        )
        self._history: dict[str, list[_Record]] = defaultdict(list)
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        """Subscribe to EvidenceProduced so aggregation happens automatically."""
        self._event_bus = event_bus
        event_bus.subscribe(EvidenceProduced, self._on_evidence, name="evidence_aggregator")
        log.info("evidence_aggregator_attached", freshness_window_seconds=self._freshness_window)

    # ---------------------------------------------------------------- handler

    async def _on_evidence(self, event: EvidenceProduced) -> None:
        evidence = event.evidence
        symbol = evidence.symbol or "UNKNOWN"
        now = _utcnow()
        group_key = f"{evidence.source}:{evidence.title}"

        history = self._history[symbol]
        history.append(_Record(evidence=evidence, received_at=now, group_key=group_key))
        if len(history) > self._max_history:
            del history[: len(history) - self._max_history]

        fresh = [r for r in history if self._freshness(r, now) > 0]
        by_group: dict[str, list[_Record]] = defaultdict(list)
        for record in fresh:
            by_group[record.group_key].append(record)

        active_evidence: list[Evidence] = []
        for records in by_group.values():
            records.sort(key=lambda r: r.received_at)
            active_evidence.append(records[-1].evidence)  # most recent representative wins

        bullish = sum(1 for e in active_evidence if e.direction == "bullish")
        bearish = sum(1 for e in active_evidence if e.direction == "bearish")
        has_conflict = bullish > 0 and bearish > 0

        this_group_records = by_group.get(group_key, [])
        occurrence_count = len(this_group_records)
        first_seen_at = this_group_records[0].received_at if this_group_records else now
        freshness = self._freshness(_Record(evidence=evidence, received_at=now, group_key=group_key), now)

        enrichment = EnrichmentInfo(
            group_key=group_key,
            occurrence_count=occurrence_count,
            is_duplicate=occurrence_count > 1,
            freshness=freshness,
            is_fresh=freshness > 0,
            first_seen_at=first_seen_at,
            age_seconds=(now - first_seen_at).total_seconds(),
        )

        if self._event_bus is not None:
            await self._event_bus.publish(
                EvidenceAggregated(
                    source="EvidenceAggregator",
                    symbol=symbol,
                    evidence=evidence,
                    enrichment=enrichment.model_dump(mode="json"),
                    active_evidence=active_evidence,
                    has_conflict=has_conflict,
                )
            )

        log.debug(
            "evidence_aggregated",
            symbol=symbol,
            group_key=group_key,
            occurrence_count=occurrence_count,
            active_evidence_count=len(active_evidence),
            has_conflict=has_conflict,
        )

    # ---------------------------------------------------------------- queries

    def _freshness(self, record: _Record, now: datetime) -> float:
        if self._freshness_window <= 0:
            return 1.0
        age = (now - record.received_at).total_seconds()
        return max(0.0, 1.0 - age / self._freshness_window)

    def snapshot(self, symbol: str) -> AggregateSnapshot:
        """The current deduped, fresh view for ``symbol`` — the same thing
        attached to the next ``EvidenceAggregated`` event, computable
        on-demand without waiting for one."""
        now = _utcnow()
        history = self._history.get(symbol, [])
        fresh = [r for r in history if self._freshness(r, now) > 0]
        by_group: dict[str, list[_Record]] = defaultdict(list)
        for record in fresh:
            by_group[record.group_key].append(record)

        active_evidence: list[Evidence] = []
        for records in by_group.values():
            records.sort(key=lambda r: r.received_at)
            active_evidence.append(records[-1].evidence)

        bullish = sum(1 for e in active_evidence if e.direction == "bullish")
        bearish = sum(1 for e in active_evidence if e.direction == "bearish")
        neutral = sum(1 for e in active_evidence if e.direction == "neutral")

        return AggregateSnapshot(
            symbol=symbol,
            active_evidence=active_evidence,
            bullish_count=bullish,
            bearish_count=bearish,
            neutral_count=neutral,
            has_conflict=bullish > 0 and bearish > 0,
        )

    def history(self, symbol: str) -> list[Evidence]:
        """The full, unfiltered historical sequence of evidence ever
        received for ``symbol`` (bounded by ``max_history_per_symbol``) —
        nothing is discarded here, unlike ``snapshot()``'s deduped view."""
        return [r.evidence for r in self._history.get(symbol, [])]
