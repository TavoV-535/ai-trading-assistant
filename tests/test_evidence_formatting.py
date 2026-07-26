"""Tests for the evidence-line text convention shared between the
Simulation Engine (writer) and the Reflection Engine (reader) --
see ``app/evidence/formatting.py``."""
from __future__ import annotations

from app.evidence.formatting import EvidenceLineParts, format_evidence_line, parse_evidence_line
from app.evidence.schema import Evidence


def _evidence(**overrides) -> Evidence:
    defaults = dict(
        source="EMA",
        category="Trend",
        title="Bullish EMA Cross",
        score=1.0,
        confidence=80.0,
        direction="bullish",
        symbol="NVDA",
    )
    defaults.update(overrides)
    return Evidence(**defaults)


def test_format_evidence_line_matches_the_documented_convention():
    line = format_evidence_line(_evidence())
    assert line == "EMA: Bullish EMA Cross (bullish, 80/100)"


def test_format_evidence_line_rounds_confidence_to_whole_number():
    line = format_evidence_line(_evidence(confidence=66.6))
    assert line == "EMA: Bullish EMA Cross (bullish, 67/100)"


def test_parse_evidence_line_round_trips_format_evidence_line():
    evidence = _evidence(source="News", title="NVDA beats estimates", direction="bearish", confidence=41.0)
    line = format_evidence_line(evidence)

    parsed = parse_evidence_line(line)

    assert parsed == EvidenceLineParts(source="News", title="NVDA beats estimates", direction="bearish", confidence=41.0)


def test_parse_evidence_line_handles_titles_containing_colons():
    # The regex splits on the *first* colon only (source), so a title with
    # its own colon must still parse correctly.
    line = "News: NVDA: record quarter (bullish, 55/100)"
    parsed = parse_evidence_line(line)
    assert parsed is not None
    assert parsed.source == "News"
    assert parsed.title == "NVDA: record quarter"
    assert parsed.direction == "bullish"
    assert parsed.confidence == 55.0


def test_parse_evidence_line_returns_none_for_malformed_input():
    assert parse_evidence_line("this is just a free-text user note") is None
    assert parse_evidence_line("EMA: Bullish EMA Cross (sideways, 80/100)") is None  # invalid direction
    assert parse_evidence_line("EMA: Bullish EMA Cross bullish, 80/100") is None  # missing parens
    assert parse_evidence_line("") is None
