"""
The evidence-line text convention.

``DecisionRecorded.technical_evidence`` / ``.fundamental_evidence``
(``app/simulation/engine.py``) don't carry raw ``Evidence`` objects — they
carry human-readable summary strings, one per active piece of evidence at
decision time:

    "EMA: Bullish EMA Cross (bullish, 80/100)"

This module is the single source of truth for that format — both the code
that *builds* it (the Simulation Engine, formatting an ``Evidence`` object
into a line for a ``DecisionRecorded`` event) and the code that *reads* it
back (the Reflection Engine, ``app/reflection/engine.py``, splitting a
decision's evidence into "supporting" vs. "contradictory" by parsing each
line's direction) share one implementation, instead of the parser
independently guessing at a format the formatter happens to produce. This
is the same "no duplicate calculations" discipline ``app/indicators/math.py``
established for indicator formulas, applied to a text convention instead of
a numeric one.

The Reflection Engine has to parse rather than receive structured data
because it only ever sees the ``DecisionRecorded`` event on the bus — by
design (see that event's docstring), not a live reference to the Evidence
Aggregator it could ask for the original ``Evidence`` objects directly.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from app.evidence.schema import Direction, Evidence

_EVIDENCE_LINE_RE = re.compile(
    r"^(?P<source>[^:]+): (?P<title>.+) \((?P<direction>bullish|bearish|neutral), (?P<confidence>\d+(?:\.\d+)?)/100\)$"
)


class EvidenceLineParts(NamedTuple):
    """The fields recovered by :func:`parse_evidence_line`."""

    source: str
    title: str
    direction: Direction
    confidence: float


def format_evidence_line(evidence: Evidence) -> str:
    """``Evidence`` -> ``"{source}: {title} ({direction}, {confidence:.0f}/100)"``.

    The one place this exact format is written — everything that needs an
    evidence line (today: ``SimulationEngine._build_decision``) calls this
    rather than building the f-string itself."""
    return f"{evidence.source}: {evidence.title} ({evidence.direction}, {evidence.confidence:.0f}/100)"


def parse_evidence_line(line: str) -> EvidenceLineParts | None:
    """The inverse of :func:`format_evidence_line`. Returns ``None`` for
    any line that doesn't match the convention exactly (a hand-written
    user note that happens to be stored alongside evidence lines, a future
    format change, ...) rather than raising — a parse miss should degrade
    gracefully (that line just isn't classified as supporting/
    contradictory), never crash the Reflection Engine."""
    match = _EVIDENCE_LINE_RE.match(line)
    if match is None:
        return None
    return EvidenceLineParts(
        source=match.group("source"),
        title=match.group("title"),
        direction=match.group("direction"),  # type: ignore[arg-type]
        confidence=float(match.group("confidence")),
    )
