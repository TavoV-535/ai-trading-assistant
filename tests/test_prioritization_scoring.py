"""
Tests for the Event Prioritization Engine's alert-scoring math
(app/prioritization/scoring.py) — each factor exercised in isolation.
"""
from __future__ import annotations

from app.prioritization.scoring import PrioritizationScoringConfig, compute_alert_score, urgency_label


def test_bare_minimum_candidate_scores_just_its_base_importance():
    config = PrioritizationScoringConfig()
    score, breakdown = compute_alert_score(
        base_importance=10.0, novelty=0.0, confidence_trend="unknown", urgency=0.0,
        in_watchlist=False, config=config,
    )
    assert score == 10.0
    assert breakdown["importance"] == 10.0
    assert breakdown["novelty"] == 0.0
    assert breakdown["confidence_change"] == 0.0
    assert breakdown["urgency"] == 0.0
    assert breakdown["user_relevance"] == 0.0


def test_novelty_scales_linearly_with_the_zero_to_one_input():
    config = PrioritizationScoringConfig()
    half = compute_alert_score(
        base_importance=0.0, novelty=0.5, confidence_trend="unknown", urgency=0.0, in_watchlist=False, config=config,
    )
    full = compute_alert_score(
        base_importance=0.0, novelty=1.0, confidence_trend="unknown", urgency=0.0, in_watchlist=False, config=config,
    )
    assert half[1]["novelty"] == round(0.5 * config.novelty_points, 2)
    assert full[1]["novelty"] == config.novelty_points


def test_confidence_rising_scores_higher_than_falling_which_scores_higher_than_stable():
    config = PrioritizationScoringConfig()
    kwargs = dict(base_importance=0.0, novelty=0.0, urgency=0.0, in_watchlist=False, config=config)
    rising = compute_alert_score(confidence_trend="rising", **kwargs)
    falling = compute_alert_score(confidence_trend="falling", **kwargs)
    stable = compute_alert_score(confidence_trend="stable", **kwargs)
    unknown = compute_alert_score(confidence_trend="unknown", **kwargs)
    assert rising[0] > falling[0] > stable[0] == unknown[0]
    assert rising[1]["confidence_change"] == config.confidence_rising_points
    assert falling[1]["confidence_change"] == config.confidence_falling_points


def test_urgency_scales_linearly_with_the_zero_to_one_input():
    config = PrioritizationScoringConfig()
    low = compute_alert_score(
        base_importance=0.0, novelty=0.0, confidence_trend="unknown", urgency=0.2, in_watchlist=False, config=config,
    )
    high = compute_alert_score(
        base_importance=0.0, novelty=0.0, confidence_trend="unknown", urgency=1.0, in_watchlist=False, config=config,
    )
    assert low[1]["urgency"] == round(0.2 * config.urgency_points, 2)
    assert high[1]["urgency"] == config.urgency_points


def test_user_relevance_is_a_flat_bonus_only_when_on_watchlist():
    config = PrioritizationScoringConfig()
    off = compute_alert_score(
        base_importance=0.0, novelty=0.0, confidence_trend="unknown", urgency=0.0, in_watchlist=False, config=config,
    )
    on = compute_alert_score(
        base_importance=0.0, novelty=0.0, confidence_trend="unknown", urgency=0.0, in_watchlist=True, config=config,
    )
    assert off[1]["user_relevance"] == 0.0
    assert on[1]["user_relevance"] == config.user_relevance_points


def test_novelty_and_urgency_inputs_are_clamped_to_zero_one_range():
    config = PrioritizationScoringConfig()
    over = compute_alert_score(
        base_importance=0.0, novelty=5.0, confidence_trend="unknown", urgency=5.0, in_watchlist=False, config=config,
    )
    under = compute_alert_score(
        base_importance=0.0, novelty=-5.0, confidence_trend="unknown", urgency=-5.0, in_watchlist=False, config=config,
    )
    assert over[1]["novelty"] == config.novelty_points
    assert over[1]["urgency"] == config.urgency_points
    assert under[1]["novelty"] == 0.0
    assert under[1]["urgency"] == 0.0


def test_score_is_clamped_to_zero_and_one_hundred():
    config = PrioritizationScoringConfig()
    score, _ = compute_alert_score(
        base_importance=1000.0, novelty=1.0, confidence_trend="rising", urgency=1.0, in_watchlist=True, config=config,
    )
    assert score == 100.0


def test_a_realistic_strategy_match_on_watchlist_clears_a_typical_threshold():
    config = PrioritizationScoringConfig()
    score, _ = compute_alert_score(
        base_importance=config.strategy_match_importance, novelty=1.0, confidence_trend="rising",
        urgency=0.6, in_watchlist=True, config=config,
    )
    assert score >= 60.0


def test_weak_repeated_evidence_stays_well_below_a_typical_threshold():
    config = PrioritizationScoringConfig()
    # Low importance (weak weight), low novelty (5th repeat), stable trend, low urgency.
    score, _ = compute_alert_score(
        base_importance=0.3 * config.evidence_importance_scale, novelty=1 / 5, confidence_trend="stable",
        urgency=0.1, in_watchlist=True, config=config,
    )
    assert score < 40.0


def test_urgency_label_maps_score_ranges():
    assert urgency_label(95) == "critical"
    assert urgency_label(80) == "high"
    assert urgency_label(50) == "normal"
    assert urgency_label(10) == "low"
