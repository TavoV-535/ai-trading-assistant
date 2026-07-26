"""
The Strategy Analytics service.

Per PROJECT.md's Milestone 12 spec: "Create long-term analytics for
every strategy... These should become reusable services rather than
command-specific calculations." Not a plugin — a core service, the same
tier as the Decision Timeline. Joins two independent event streams by
``decision_event_id`` without either producer knowing this service
exists:

- ``DecisionRecorded`` (``app/timeline/``, Milestone 9) — which
  strategies matched, confidence, market context, evidence sources, and
  the resolved outcome.
- ``TradeClosed`` (``app/capital_protection/``, Milestone 11) — the real
  synthesized ``pnl`` for the trade that decision produced, when one was
  opened. A decision the Capital Protection Engine never turned into a
  trade (e.g. ``no_action``) simply has no matching pnl — win rate still
  comes from ``DecisionRecorded.outcome`` either way.

Real currency pnl is preferred over ``outcome_price_change_pct`` wherever
available (profit factor, expectancy, max drawdown) because it already
reflects the Capital Protection Engine's confidence/profile-scaled
position sizing — a $500 win on an oversized position and a $500 win on a
tiny one both count as "one win" for ``win_rate``, but pnl-based
statistics correctly weigh them differently.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, RiskEvent, TradeClosed
from app.evidence.formatting import parse_evidence_line
from app.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MAX_PER_STRATEGY = 1000


@dataclass
class _DecisionRecord:
    event_id: UUID
    symbol: str
    confidence: float
    outcome: str | None
    outcome_pending: bool
    lookahead_bars: int
    market_context: dict[str, str] = field(default_factory=dict)
    evidence_sources: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    risk_profile: str | None = None


class StrategyStats(BaseModel):
    strategy: str
    sample_size: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    expectancy: float | None = None
    average_hold_time_bars: float | None = None
    average_r: float | None = None
    max_drawdown: float | None = None
    context_performance: dict[str, float] = Field(default_factory=dict)
    volatility_performance: dict[str, float] = Field(default_factory=dict)
    confidence_distribution: dict[str, int] = Field(default_factory=dict)
    risk_profile_performance: dict[str, float] = Field(default_factory=dict)
    evidence_distribution: dict[str, int] = Field(default_factory=dict)
    #: "improving" | "declining" | "stable" | "insufficient_data" -- win
    #: rate of the second half of history vs. the first half.
    historical_trend: str = "insufficient_data"


def _win_rate(records: list[_DecisionRecord]) -> float:
    resolved = [r for r in records if not r.outcome_pending and r.outcome in ("correct", "incorrect")]
    if not resolved:
        return 0.0
    return sum(1 for r in resolved if r.outcome == "correct") / len(resolved)


class StrategyAnalyticsService:
    """Maintains a bounded, per-strategy history joining
    ``DecisionRecorded`` and ``TradeClosed`` and produces
    :class:`StrategyStats` on demand via :meth:`stats_for`/:meth:`all`.
    Attach once at bootstrap (or once per Simulation Engine run); every
    consumer (the Learning Engine, the Analytics Service, ``/coach``,
    ``/analyze``) reads it via the same read-only-query pattern every
    other core engine here exposes."""

    def __init__(self, settings: Any, *, max_per_strategy: int | None = None) -> None:
        section = getattr(settings, "learning", None)
        self._max_per_strategy = max_per_strategy if max_per_strategy is not None else int(
            getattr(section, "strategy_analytics_max_per_strategy", _DEFAULT_MAX_PER_STRATEGY)
        )
        self._records: dict[str, list[_DecisionRecord]] = defaultdict(list)
        self._pnl_by_decision: dict[UUID, float] = {}
        self._active_profile_by_symbol: dict[str, str] = {}
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="strategy_analytics_decisions")
        event_bus.subscribe(TradeClosed, self._on_trade_closed, name="strategy_analytics_trades")
        event_bus.subscribe(RiskEvent, self._on_risk_event, name="strategy_analytics_risk")
        log.info("strategy_analytics_service_attached", max_per_strategy=self._max_per_strategy)

    async def _on_risk_event(self, event: RiskEvent) -> None:
        # Cache-only, same pattern the Reflection Engine and Knowledge
        # Graph already use for cross-engine data -- never a live call
        # into the Capital Protection Engine.
        if event.profile_name:
            self._active_profile_by_symbol[event.symbol or ""] = event.profile_name

    async def _on_trade_closed(self, event: TradeClosed) -> None:
        if event.decision_event_id is not None and event.pnl is not None:
            self._pnl_by_decision[event.decision_event_id] = event.pnl

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        if not event.strategy_matches:
            return
        sources = []
        for line in list(event.technical_evidence) + list(event.fundamental_evidence):
            parsed = parse_evidence_line(line)
            if parsed is not None:
                sources.append(parsed.source)
        record = _DecisionRecord(
            event_id=event.event_id,
            symbol=event.symbol,
            confidence=event.confidence,
            outcome=event.outcome,
            outcome_pending=event.outcome_pending,
            lookahead_bars=event.lookahead_bars,
            market_context=dict(event.market_context),
            evidence_sources=sources,
            timestamp=event.timestamp,
            risk_profile=self._active_profile_by_symbol.get(event.symbol) or self._active_profile_by_symbol.get(""),
        )
        for strategy in event.strategy_matches:
            bucket = self._records[strategy]
            bucket.append(record)
            if len(bucket) > self._max_per_strategy:
                del bucket[0]

    # ---------------------------------------------------------------- queries

    def stats_for(self, strategy: str) -> StrategyStats | None:
        records = self._records.get(strategy)
        if not records:
            return None

        resolved = [r for r in records if not r.outcome_pending and r.outcome in ("correct", "incorrect")]
        win_rate = _win_rate(records)

        pnls = [self._pnl_by_decision[r.event_id] for r in records if r.event_id in self._pnl_by_decision]
        profit_factor = None
        expectancy = None
        average_r = None
        max_drawdown = None
        if pnls:
            gains = sum(p for p in pnls if p > 0)
            losses = sum(p for p in pnls if p < 0)
            profit_factor = (gains / abs(losses)) if losses < 0 else (float("inf") if gains > 0 else None)
            expectancy = sum(pnls) / len(pnls)
            magnitudes = [abs(p) for p in pnls if p != 0]
            average_r = (sum(1 for p in pnls if p > 0) - sum(1 for p in pnls if p < 0)) / len(pnls) if pnls else None
            if magnitudes:
                average_r = expectancy / (sum(magnitudes) / len(magnitudes))
            cumulative = 0.0
            peak = 0.0
            drawdown = 0.0
            for p in pnls:
                cumulative += p
                peak = max(peak, cumulative)
                drawdown = min(drawdown, cumulative - peak)
            max_drawdown = drawdown

        context_perf: dict[str, list[_DecisionRecord]] = defaultdict(list)
        volatility_perf: dict[str, list[_DecisionRecord]] = defaultdict(list)
        for r in records:
            for context_type, label in r.market_context.items():
                context_perf[label].append(r)
                if context_type == "volatility":
                    volatility_perf[label].append(r)

        confidence_dist: dict[str, int] = defaultdict(int)
        for r in records:
            bucket = int(r.confidence // 10) * 10
            confidence_dist[f"{bucket}-{bucket + 10}%"] += 1

        risk_profile_perf: dict[str, list[_DecisionRecord]] = defaultdict(list)
        for r in records:
            if r.risk_profile:
                risk_profile_perf[r.risk_profile].append(r)

        evidence_dist: dict[str, int] = defaultdict(int)
        for r in records:
            for source in r.evidence_sources:
                evidence_dist[source] += 1

        hold_times = [r.lookahead_bars for r in records if r.lookahead_bars]
        avg_hold = (sum(hold_times) / len(hold_times)) if hold_times else None

        trend = "insufficient_data"
        if len(resolved) >= 4:
            ordered = sorted(resolved, key=lambda r: r.timestamp)
            mid = len(ordered) // 2
            first_rate = _win_rate(ordered[:mid])
            second_rate = _win_rate(ordered[mid:])
            if second_rate - first_rate > 0.05:
                trend = "improving"
            elif first_rate - second_rate > 0.05:
                trend = "declining"
            else:
                trend = "stable"

        return StrategyStats(
            strategy=strategy,
            sample_size=len(records),
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            average_hold_time_bars=avg_hold,
            average_r=average_r,
            max_drawdown=max_drawdown,
            context_performance={label: _win_rate(rs) for label, rs in context_perf.items()},
            volatility_performance={label: _win_rate(rs) for label, rs in volatility_perf.items()},
            confidence_distribution=dict(confidence_dist),
            risk_profile_performance={p: _win_rate(rs) for p, rs in risk_profile_perf.items()},
            evidence_distribution=dict(evidence_dist),
            historical_trend=trend,
        )

    def all(self) -> list[StrategyStats]:
        return [s for s in (self.stats_for(name) for name in sorted(self._records)) if s is not None]

    def strategies(self) -> list[str]:
        return sorted(self._records.keys())

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "strategies_tracked": len(self._records)}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "strategies_tracked": sorted(self._records.keys()),
            "total_decisions_tracked": sum(len(v) for v in self._records.values()),
            "trades_with_pnl": len(self._pnl_by_decision),
        }

    def statistics(self) -> dict[str, Any]:
        return {
            "strategies_tracked": len(self._records),
            "total_decisions_tracked": sum(len(v) for v in self._records.values()),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
