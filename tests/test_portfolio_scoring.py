"""
Tests for the Portfolio Intelligence Layer's priority-scoring math
(app/portfolio/scoring.py) — each factor exercised in isolation, mirroring
the same per-factor style tests/test_confidence_weighting.py uses for the
Confidence Weighting Framework.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.portfolio.scoring import PortfolioScoringConfig, compute_priority


def _now():
    return datetime.now(timezone.utc)


def test_all_factors_zero_when_nothing_is_active():
    score, breakdown = compute_priority(
        top_weight=0.0,
        has_fresh_fundamental_evidence=False,
        context={},
        confidence_trend="unknown",
        matched_strategies=[],
        last_alert_at=None,
        now=_now(),
        config=PortfolioScoringConfig(),
    )
    assert score == 0.0
    assert breakdown["evidence_strength"] == 0.0
    assert breakdown["fundamental_freshness"] == 0.0
    assert breakdown["context_intensity"] == 0.0
    assert breakdown["confidence_trend"] == 0.0
    assert breakdown["strategy_match"] == 0.0
    assert breakdown["alert_suppression_factor"] == 1.0


def test_evidence_strength_scales_with_top_weight():
    config = PortfolioScoringConfig()
    weak = compute_priority(
        top_weight=0.2, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    strong = compute_priority(
        top_weight=1.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    assert weak[1]["evidence_strength"] == round(0.2 * config.evidence_strength_points, 2)
    assert strong[1]["evidence_strength"] == config.evidence_strength_points
    assert strong[0] > weak[0]


def test_top_weight_is_clamped_to_one_even_if_caller_passes_more():
    config = PortfolioScoringConfig()
    score, breakdown = compute_priority(
        top_weight=5.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    assert breakdown["evidence_strength"] == config.evidence_strength_points


def test_fresh_fundamental_evidence_adds_flat_bonus():
    config = PortfolioScoringConfig()
    without = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    with_it = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=True, context={}, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    assert with_it[1]["fundamental_freshness"] == config.fundamental_freshness_points
    assert with_it[0] - without[0] == config.fundamental_freshness_points


def test_context_intensity_counts_only_notable_types_and_is_capped():
    config = PortfolioScoringConfig()
    context = {"trend": "Bull Trend", "volatility": "High Volatility", "liquidity": "Low Liquidity"}
    # "liquidity" isn't in the default notable_context_types list -- only
    # trend + volatility should count, at 5 points each.
    _, breakdown = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context=context, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    assert breakdown["context_intensity"] == 2 * config.context_points_per_label

    # Many notable labels still cap at context_points_cap.
    lots_of_context = {t: "x" for t in config.notable_context_types}
    _, capped = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context=lots_of_context, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    assert capped["context_intensity"] == config.context_points_cap


def test_confidence_trend_rising_scores_higher_than_falling_which_scores_higher_than_stable():
    config = PortfolioScoringConfig()
    rising = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="rising",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    falling = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="falling",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    stable = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="stable",
        matched_strategies=[], last_alert_at=None, now=_now(), config=config,
    )
    assert rising[0] > falling[0] > stable[0]
    assert rising[1]["confidence_trend"] == config.confidence_rising_points
    assert falling[1]["confidence_trend"] == config.confidence_falling_points
    assert stable[1]["confidence_trend"] == 0.0


def test_strategy_match_adds_flat_bonus_regardless_of_count():
    config = PortfolioScoringConfig()
    one = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=["Momentum Breakout"], last_alert_at=None, now=_now(), config=config,
    )
    two = compute_priority(
        top_weight=0.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=["Momentum Breakout", "Another Strategy"], last_alert_at=None, now=_now(), config=config,
    )
    assert one[1]["strategy_match"] == config.strategy_match_points
    assert one[0] == two[0]  # flat bonus, not per-strategy


def test_recent_alert_dampens_but_does_not_zero_the_score():
    config = PortfolioScoringConfig(alert_suppression_seconds=300.0, alert_suppression_factor=0.5)
    now = _now()
    raw_score, _ = compute_priority(
        top_weight=1.0, has_fresh_fundamental_evidence=True, context={}, confidence_trend="rising",
        matched_strategies=["X"], last_alert_at=None, now=now, config=config,
    )
    dampened_score, breakdown = compute_priority(
        top_weight=1.0, has_fresh_fundamental_evidence=True, context={}, confidence_trend="rising",
        matched_strategies=["X"], last_alert_at=now - timedelta(seconds=10), now=now, config=config,
    )
    assert breakdown["alert_suppression_factor"] == 0.5
    assert dampened_score == round(raw_score * 0.5, 2)
    assert dampened_score > 0.0  # dampened, never hard-zeroed


def test_old_alert_outside_suppression_window_does_not_dampen():
    config = PortfolioScoringConfig(alert_suppression_seconds=300.0, alert_suppression_factor=0.5)
    now = _now()
    score, breakdown = compute_priority(
        top_weight=1.0, has_fresh_fundamental_evidence=False, context={}, confidence_trend="unknown",
        matched_strategies=[], last_alert_at=now - timedelta(seconds=600), now=now, config=config,
    )
    assert breakdown["alert_suppression_factor"] == 1.0
    assert score == PortfolioScoringConfig().evidence_strength_points


def test_score_is_clamped_to_zero_and_one_hundred():
    config = PortfolioScoringConfig()
    score, _ = compute_priority(
        top_weight=1.0, has_fresh_fundamental_evidence=True,
        context={t: "x" for t in config.notable_context_types}, confidence_trend="rising",
        matched_strategies=["X"], last_alert_at=None, now=_now(), config=config,
    )
    assert 0.0 <= score <= 100.0


def test_from_settings_reads_portfolio_section():
    class _Section:
        notable_context_types = ["trend"]
        alert_suppression_seconds = 123.0
        alert_suppression_factor = 0.25

    class _Settings:
        portfolio = _Section()

    config = PortfolioScoringConfig.from_settings(_Settings())
    assert config.notable_context_types == ("trend",)
    assert config.alert_suppression_seconds == 123.0
    assert config.alert_suppression_factor == 0.25


def test_from_settings_falls_back_to_defaults_when_no_portfolio_section():
    class _Settings:
        pass

    config = PortfolioScoringConfig.from_settings(_Settings())
    assert config == PortfolioScoringConfig()
