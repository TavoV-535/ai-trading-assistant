"""
Unit + focused-integration tests for the Unified Simulation Engine
(``app/simulation/``). See ``tests/test_milestone9_pipeline_integration.py``
for the full end-to-end pipeline demonstration (indicators -> aggregator ->
strategy engine -> context/portfolio/prioritization -> reasoning ->
Decision Timeline, plus ``/analyze`` run against a simulation's engines).
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.aggregation.models import AggregateSnapshot
from app.event_bus.events import DecisionRecorded, WeightedEvidenceEvent
from app.evidence.schema import Evidence, EvidenceCategory
from app.reasoning.engine import ReasoningOutput
from app.simulation import SimulationConfig, SimulationEngine
from app.simulation.engine import _PendingDecision, _infer_action, _resolve_outcome

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _output(source: str = "evidence_only", confidence: float = 60.0) -> ReasoningOutput:
    return ReasoningOutput(
        market_summary="summary",
        trade_thesis="thesis",
        risk_assessment="risk",
        alternative_scenario="alt",
        confidence=confidence,
        source=source,
    )


def _snapshot(*, bullish_count=0, bearish_count=0, weighted=None) -> AggregateSnapshot:
    return AggregateSnapshot(
        symbol="NVDA",
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        weighted_evidence=weighted or [],
    )


def _weighted(direction: str, weight: float) -> WeightedEvidenceEvent:
    evidence = Evidence(
        source="EMA", category=EvidenceCategory.TREND, title="x", score=1, confidence=80, direction=direction
    )
    return WeightedEvidenceEvent(evidence=evidence, weight=weight)


# ---------------------------------------------------------------- _infer_action


def test_infer_action_insufficient_evidence_is_always_no_action():
    output = _output(source="insufficient_evidence")
    snapshot = _snapshot(bullish_count=5, bearish_count=0)
    assert _infer_action(output, snapshot) == "no_action"


def test_infer_action_uses_weighted_mass_when_available():
    output = _output()
    snapshot = _snapshot(weighted=[_weighted("bullish", 0.8), _weighted("bearish", 0.2)])
    assert _infer_action(output, snapshot) == "watch_bullish"

    snapshot = _snapshot(weighted=[_weighted("bullish", 0.2), _weighted("bearish", 0.8)])
    assert _infer_action(output, snapshot) == "watch_bearish"


def test_infer_action_falls_back_to_raw_counts_without_weighted_evidence():
    output = _output()
    assert _infer_action(output, _snapshot(bullish_count=3, bearish_count=1)) == "watch_bullish"
    assert _infer_action(output, _snapshot(bullish_count=1, bearish_count=3)) == "watch_bearish"
    assert _infer_action(output, _snapshot(bullish_count=2, bearish_count=2)) == "watch_neutral"


def test_infer_action_never_returns_buy_or_sell():
    # Explicit guard for the platform's "never a signal-selling bot" rule.
    for bullish_count, bearish_count in [(5, 0), (0, 5), (2, 2)]:
        action = _infer_action(_output(), _snapshot(bullish_count=bullish_count, bearish_count=bearish_count))
        assert action not in {"buy", "sell"}
        assert action.startswith("watch_") or action == "no_action"


# ---------------------------------------------------------------- _resolve_outcome


def _pending(action: str, entry_price: float = 100.0) -> _PendingDecision:
    event = DecisionRecorded(source="SimulationEngine", symbol="NVDA", simulated_action=action, bar_index=0, lookahead_bars=10)
    return _PendingDecision(event=event, bar_index=0, symbol="NVDA", entry_price=entry_price)


def test_resolve_outcome_bullish_correct_when_price_rises():
    resolved = _resolve_outcome(_pending("watch_bullish"), prices=[100.0, 105.0], neutral_band_pct=0.05)
    assert resolved.outcome == "correct"
    assert resolved.outcome_pending is False
    assert resolved.outcome_price_change_pct == pytest.approx(5.0)


def test_resolve_outcome_bullish_incorrect_when_price_falls():
    resolved = _resolve_outcome(_pending("watch_bullish"), prices=[100.0, 95.0], neutral_band_pct=0.05)
    assert resolved.outcome == "incorrect"


def test_resolve_outcome_bearish_correct_when_price_falls():
    resolved = _resolve_outcome(_pending("watch_bearish"), prices=[100.0, 95.0], neutral_band_pct=0.05)
    assert resolved.outcome == "correct"


def test_resolve_outcome_bearish_incorrect_when_price_rises():
    resolved = _resolve_outcome(_pending("watch_bearish"), prices=[100.0, 105.0], neutral_band_pct=0.05)
    assert resolved.outcome == "incorrect"


def test_resolve_outcome_small_moves_are_neutral_regardless_of_direction():
    resolved = _resolve_outcome(_pending("watch_bullish"), prices=[100.0, 100.02], neutral_band_pct=0.05)
    assert resolved.outcome == "neutral"


def test_resolve_outcome_watch_neutral_always_resolves_neutral():
    resolved = _resolve_outcome(_pending("watch_neutral"), prices=[100.0, 150.0], neutral_band_pct=0.05)
    assert resolved.outcome == "neutral"


def test_resolve_outcome_no_action_has_no_outcome_and_is_not_pending():
    resolved = _resolve_outcome(_pending("no_action"), prices=[100.0, 150.0], neutral_band_pct=0.05)
    assert resolved.outcome is None
    assert resolved.outcome_price_change_pct is None
    assert resolved.outcome_pending is False


def test_resolve_outcome_never_mutates_the_original_event():
    pending = _pending("watch_bullish")
    original = pending.event
    _resolve_outcome(pending, prices=[100.0, 105.0], neutral_band_pct=0.05)
    assert original.outcome is None
    assert original.outcome_pending is True


# ---------------------------------------------------------------- SimulationConfig validation


async def test_simulation_run_rejects_empty_symbols(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    with pytest.raises(ValueError, match="symbols must not be empty"):
        await engine.run(SimulationConfig(symbols=[]))


async def test_simulation_run_dedupes_symbols_preserving_order(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(SimulationConfig(symbols=["NVDA", "AAPL", "NVDA"], bar_count=10))
    assert result.symbols == ["NVDA", "AAPL"]


# ---------------------------------------------------------------- engine behavior


async def test_simulation_run_processes_the_configured_bar_count(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(SimulationConfig(symbols=["NVDA"], bar_count=25))
    assert result.bars_processed == 25


async def test_simulation_run_respects_decision_and_lookahead_overrides(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(
        SimulationConfig(symbols=["NVDA"], bar_count=40, decision_interval_bars=10, lookahead_bars=5)
    )
    records = result.decision_timeline.for_symbol("NVDA")
    assert records  # at least one decision recorded
    for record in records:
        assert record.bar_index % 10 == 0
        assert record.lookahead_bars == 5


async def test_simulation_run_records_a_decision_per_watched_symbol(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(SimulationConfig(symbols=["NVDA", "AAPL"], bar_count=30, decision_interval_bars=5))
    assert result.decision_timeline.for_symbol("NVDA")
    assert result.decision_timeline.for_symbol("AAPL")


async def test_simulation_run_resolves_outcomes_once_lookahead_elapses(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(
        SimulationConfig(symbols=["NVDA"], bar_count=60, decision_interval_bars=5, lookahead_bars=10)
    )
    records = result.decision_timeline.for_symbol("NVDA")
    # "no_action" decisions (no evidence yet, e.g. bar 0) have no direction
    # to grade and resolve to outcome=None/outcome_pending=False immediately
    # -- that's the honest "nothing to wait for" case, not an unresolved
    # directional call. Only directional ("watch_*") decisions are expected
    # to eventually carry a real correct/incorrect/neutral verdict.
    directional_resolved = [
        r for r in records if not r.outcome_pending and r.simulated_action != "no_action"
    ]
    assert directional_resolved, "expected at least one directional decision to resolve within 60 bars"
    for record in directional_resolved:
        assert record.outcome in {"correct", "incorrect", "neutral"}
        assert record.outcome_price_change_pct is not None


async def test_simulation_run_honestly_leaves_late_decisions_pending(settings):
    # decision recorded at bar 0, but the run ends before lookahead_bars=50
    # more bars exist -- must NOT fabricate an outcome from data that
    # doesn't exist yet.
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(
        SimulationConfig(symbols=["NVDA"], bar_count=10, decision_interval_bars=5, lookahead_bars=50)
    )
    assert result.decisions_pending_at_end > 0
    records = result.decision_timeline.for_symbol("NVDA")
    pending = [r for r in records if r.outcome_pending]
    assert pending
    for record in pending:
        assert record.outcome is None
        assert record.outcome_price_change_pct is None


async def test_simulation_never_calls_a_real_ai_provider(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(SimulationConfig(symbols=["NVDA", "AAPL"], bar_count=30, decision_interval_bars=5))
    for record in result.decision_timeline.all():
        assert record.reasoning_source in {"evidence_only", "insufficient_evidence"}


async def test_simulation_scoped_settings_never_mutates_base_settings(settings):
    original_watchlist = list(settings.portfolio.watchlist)
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    await engine.run(SimulationConfig(symbols=["ZZZZ"], bar_count=5))
    assert settings.portfolio.watchlist == original_watchlist
    assert "ZZZZ" not in settings.portfolio.watchlist


async def test_simulation_include_intelligence_false_records_no_fundamental_evidence(settings):
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result = await engine.run(
        SimulationConfig(symbols=["NVDA"], bar_count=30, decision_interval_bars=5, include_intelligence=False)
    )
    for record in result.decision_timeline.for_symbol("NVDA"):
        assert record.fundamental_evidence == []


async def test_simulation_determinism_two_independent_runs_match(settings):
    """Given identical historical data and configuration, two independent
    runs must produce an identical Decision Timeline and identical alerts
    -- the milestone's core determinism requirement. ``correlation_id`` is
    pinned explicitly: it's part of the run's configuration (auto-generated
    only when the caller omits it -- see SimulationConfig's docstring), so
    "identical configuration" means pinning it here the same way a real
    reproducibility check would."""
    config = SimulationConfig(
        symbols=["NVDA", "AAPL"],
        bar_count=50,
        decision_interval_bars=5,
        lookahead_bars=10,
        correlation_id=uuid4(),
    )

    engine_a = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result_a = await engine_a.run(config)

    engine_b = SimulationEngine(settings, project_root=PROJECT_ROOT)
    result_b = await engine_b.run(config)

    assert result_a.bars_processed == result_b.bars_processed
    assert result_a.decisions_recorded == result_b.decisions_recorded
    assert result_a.decisions_pending_at_end == result_b.decisions_pending_at_end

    def _comparable(records):
        return [r.model_dump(exclude={"event_id"}) for r in records]

    assert _comparable(result_a.decision_timeline.all()) == _comparable(result_b.decision_timeline.all())

    def _comparable_alerts(alerts):
        return [a.model_dump(exclude={"event_id"}) for a in alerts]

    assert _comparable_alerts(result_a.alerts) == _comparable_alerts(result_b.alerts)
