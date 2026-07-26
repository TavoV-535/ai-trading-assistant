"""Unit tests for the Milestone 12 Analytics stack
(``app/analytics/``): Confidence Calibration, Evidence Reliability,
Strategy Analytics, and the composed Analytics Service facade."""
from __future__ import annotations

from uuid import uuid4

from app.analytics.calibration import ConfidenceCalibrationService
from app.analytics.evidence_reliability import EvidenceReliabilityEngine
from app.analytics.service import AnalyticsService
from app.analytics.strategy_analytics import StrategyAnalyticsService
from app.event_bus.bus import EventBus
from app.event_bus.events import DecisionRecorded, RiskEvent, TradeClosed


def _decision(**overrides) -> DecisionRecorded:
    defaults = dict(
        source="test",
        symbol="NVDA",
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
        fundamental_evidence=[],
        strategy_matches=["Momentum Breakout"],
        market_context={"volatility": "High Volatility"},
        confidence=65.0,
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        bar_index=0,
        lookahead_bars=5,
        outcome="correct",
        outcome_price_change_pct=1.0,
        outcome_pending=False,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


# ---------------------------------------------------------------- ConfidenceCalibrationService


async def test_calibration_reports_overconfident_when_win_rate_trails_confidence(settings):
    service = ConfidenceCalibrationService(settings, min_sample=2, tolerance=0.05)
    bus = EventBus.from_settings(settings)
    service.attach(bus)

    # 90% confidence bucket, but only half actually win -> overconfident.
    await bus.publish(_decision(confidence=90.0, outcome="correct"))
    await bus.publish(_decision(confidence=92.0, outcome="incorrect"))
    await bus.drain()

    report = service.report()
    bucket = next(b for b in report.buckets if b.label == "90-100%")
    assert bucket.sample_size == 2
    assert bucket.verdict == "overconfident"
    assert "overconfident" in report.overall_verdict.lower() or "optimistic" in report.overall_verdict.lower()
    await bus.shutdown()


async def test_calibration_skips_pending_and_neutral_decisions(settings):
    service = ConfidenceCalibrationService(settings, min_sample=1)
    bus = EventBus.from_settings(settings)
    service.attach(bus)

    await bus.publish(_decision(confidence=50.0, outcome=None, outcome_pending=True))
    await bus.publish(_decision(confidence=50.0, outcome="neutral", outcome_pending=False))
    await bus.drain()

    assert service.statistics()["total_observed"] == 0
    await bus.shutdown()


async def test_calibration_bucket_below_min_sample_is_insufficient_data(settings):
    service = ConfidenceCalibrationService(settings, min_sample=5)
    bus = EventBus.from_settings(settings)
    service.attach(bus)
    await bus.publish(_decision(confidence=60.0, outcome="correct"))
    await bus.drain()

    report = service.report()
    bucket = next(b for b in report.buckets if b.label == "60-70%")
    assert bucket.verdict == "insufficient_data"
    await bus.shutdown()


async def test_calibration_health_diagnostics_statistics(settings):
    service = ConfidenceCalibrationService(settings)
    health = await service.health()
    assert health["status"] == "healthy"
    diagnostics = service.diagnostics()
    assert "min_sample" in diagnostics and "tolerance" in diagnostics
    stats = service.statistics()
    assert "generated_at" in stats


# ---------------------------------------------------------------- EvidenceReliabilityEngine


async def test_evidence_reliability_tracks_correct_direction_agreement(settings):
    engine = EvidenceReliabilityEngine(settings)
    bus = EventBus.from_settings(settings)
    engine.attach(bus)

    # bullish EMA evidence + a correct bullish (watch_bullish) decision ->
    # true direction is bullish -> EMA agreed.
    await bus.publish(_decision(technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"], simulated_action="watch_bullish", outcome="correct"))
    # bearish EMA evidence + an incorrect bullish decision -> true direction
    # is actually bearish -> EMA (bearish) agreed here too.
    await bus.publish(_decision(technical_evidence=["EMA: Bearish EMA Cross (bearish, 70/100)"], simulated_action="watch_bullish", outcome="incorrect"))
    # bullish RSI evidence + an incorrect bullish decision -> true direction
    # bearish -> RSI (bullish) disagreed.
    await bus.publish(_decision(technical_evidence=["RSI: Oversold Bounce (bullish, 60/100)"], simulated_action="watch_bullish", outcome="incorrect"))
    await bus.drain()

    ema_stat = engine.for_source("EMA")
    assert ema_stat.total == 2
    assert ema_stat.correct == 2
    assert ema_stat.reliability == 1.0

    rsi_stat = engine.for_source("RSI")
    assert rsi_stat.total == 1
    assert rsi_stat.correct == 0

    ranked = engine.ranked(min_sample=1)
    assert ranked[0].source == "EMA"  # most reliable first
    await bus.shutdown()


async def test_evidence_reliability_ignores_pending_and_neutral(settings):
    engine = EvidenceReliabilityEngine(settings)
    bus = EventBus.from_settings(settings)
    engine.attach(bus)
    await bus.publish(_decision(outcome=None, outcome_pending=True))
    await bus.publish(_decision(outcome="neutral", outcome_pending=False))
    await bus.drain()
    assert engine.all() == []
    await bus.shutdown()


def test_evidence_reliability_unknown_source_returns_none(settings):
    engine = EvidenceReliabilityEngine(settings)
    assert engine.for_source("DoesNotExist") is None


# ---------------------------------------------------------------- StrategyAnalyticsService


async def test_strategy_analytics_joins_decision_recorded_and_trade_closed_by_id(settings):
    service = StrategyAnalyticsService(settings)
    bus = EventBus.from_settings(settings)
    service.attach(bus)

    decision = _decision()
    await bus.publish(decision)
    await bus.publish(TradeClosed(source="test", symbol="NVDA", exit_price=105.0, trade_id=uuid4(), pnl=50.0, decision_event_id=decision.event_id))
    await bus.drain()

    stats = service.stats_for("Momentum Breakout")
    assert stats is not None
    assert stats.sample_size == 1
    assert stats.win_rate == 1.0
    assert stats.profit_factor == float("inf")  # all-gains, no losses
    assert stats.expectancy == 50.0
    assert stats.context_performance.get("High Volatility") == 1.0
    assert stats.volatility_performance.get("High Volatility") == 1.0
    assert stats.evidence_distribution.get("EMA") == 1
    await bus.shutdown()


async def test_strategy_analytics_ignores_decisions_with_no_strategy_match(settings):
    service = StrategyAnalyticsService(settings)
    bus = EventBus.from_settings(settings)
    service.attach(bus)
    await bus.publish(_decision(strategy_matches=[]))
    await bus.drain()
    assert service.all() == []
    assert service.strategies() == []
    await bus.shutdown()


async def test_strategy_analytics_bounded_per_strategy(settings):
    service = StrategyAnalyticsService(settings, max_per_strategy=2)
    bus = EventBus.from_settings(settings)
    service.attach(bus)
    for i in range(4):
        await bus.publish(_decision(bar_index=i))
    await bus.drain()
    stats = service.stats_for("Momentum Breakout")
    assert stats.sample_size == 2  # oldest evicted
    await bus.shutdown()


async def test_strategy_analytics_caches_active_risk_profile_from_risk_events(settings):
    service = StrategyAnalyticsService(settings)
    bus = EventBus.from_settings(settings)
    service.attach(bus)
    await bus.publish(RiskEvent(source="test", symbol="NVDA", risk_type="daily_drawdown", severity="info", value=0.0, profile_name="swing_trader", message="ok"))
    await bus.drain()
    await bus.publish(_decision())
    await bus.drain()

    stats = service.stats_for("Momentum Breakout")
    assert stats.risk_profile_performance.get("swing_trader") == 1.0
    await bus.shutdown()


# ---------------------------------------------------------------- AnalyticsService (composed facade)


async def test_analytics_service_composes_without_recalculating(settings):
    calibration = ConfidenceCalibrationService(settings, min_sample=1)
    reliability = EvidenceReliabilityEngine(settings)
    strategy_analytics = StrategyAnalyticsService(settings)
    bus = EventBus.from_settings(settings)
    calibration.attach(bus)
    reliability.attach(bus)
    strategy_analytics.attach(bus)

    await bus.publish(_decision())
    await bus.drain()

    service = AnalyticsService(
        strategy_analytics=strategy_analytics, evidence_reliability=reliability, confidence_calibration=calibration
    )

    # Every read delegates straight through -- no separate calculation path.
    assert service.strategies() == strategy_analytics.strategies()
    assert service.all_strategy_stats() == strategy_analytics.all()
    assert service.strategy_stats("Momentum Breakout") == strategy_analytics.stats_for("Momentum Breakout")
    assert service.evidence_reliability_for("EMA") == reliability.for_source("EMA")
    assert service.ranked_evidence_reliability() == reliability.ranked()
    # Compare everything except `generated_at` -- each call to .report()
    # stamps its own timestamp, so the two objects are never byte-identical
    # even though they reflect the exact same underlying calculation.
    assert service.calibration_report().model_dump(exclude={"generated_at"}) == calibration.report().model_dump(exclude={"generated_at"})

    health = await service.health()
    assert health["status"] == "healthy"
    assert "strategy_analytics" in service.diagnostics()
    assert "evidence_reliability" in service.statistics()
    await bus.shutdown()
