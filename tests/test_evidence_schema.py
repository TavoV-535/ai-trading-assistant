from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.evidence import Evidence, EvidenceCategory


def test_valid_evidence_matches_spec_example():
    e = Evidence(
        source="EMA",
        category=EvidenceCategory.TREND,
        title="Bullish EMA Cross",
        score=15,
        confidence=91,
        direction="Bullish",
        metadata={"fast": 20, "slow": 50},
    )
    assert e.source == "EMA"
    assert e.direction == "bullish"  # normalized to lowercase
    assert e.metadata == {"fast": 20, "slow": 50}


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Evidence(source="EMA", category="Trend", title="x", score=1, confidence=150, direction="bullish")

    with pytest.raises(ValidationError):
        Evidence(source="EMA", category="Trend", title="x", score=1, confidence=-1, direction="bullish")


def test_invalid_direction_rejected():
    with pytest.raises(ValidationError):
        Evidence(source="EMA", category="Trend", title="x", score=1, confidence=50, direction="sideways")


def test_evidence_is_immutable():
    e = Evidence(source="EMA", category="Trend", title="x", score=1, confidence=50, direction="neutral")
    with pytest.raises(Exception):
        e.score = 99


def test_evidence_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Evidence(
            source="EMA",
            category="Trend",
            title="x",
            score=1,
            confidence=50,
            direction="neutral",
            unexpected_field="nope",
        )
