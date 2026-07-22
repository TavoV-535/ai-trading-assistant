"""
Compiles a declarative :class:`~app.strategy.models.StrategyDefinition`
into a :class:`CompiledStrategy` — a small, immutable rule graph built once
at load time, not re-parsed or re-validated on every evaluation. This is
what "compile into an internal rule graph during loading rather than being
interpreted line-by-line on every evaluation" means in practice here:
``required``/``optional`` become frozensets (O(1) membership checks
instead of list scans or YAML re-reads), and evaluation is a handful of set
operations plus a sum — no branching tree of if/elif clauses re-derived
from the raw YAML on every call.

Nothing in this module knows what an EMA, RSI, or MACD is. It only ever
reads ``Evidence.title``, ``Evidence.source``, ``Evidence.score``,
``Evidence.direction``, and ``Evidence.metadata`` — the same vocabulary any
future evidence producer (news, earnings, macro, options flow, ...)
already speaks. Adding a new indicator plugin makes its evidence titles
available to every strategy automatically; nothing here has to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.evidence.schema import Evidence
from app.strategy.models import StrategyDefinition

#: Recognized repeat-policy names. Unknown policies fail open (treated as
#: "every_breakout") rather than silently dropping evidence a strategy
#: author typo'd their way into excluding.
_REPEAT_POLICIES = {"every_breakout", "first_breakout", "after_pullback"}


class StrategyEvaluation(BaseModel):
    """The result of evaluating one compiled strategy against one symbol's
    current active evidence snapshot."""

    strategy: str
    matched: bool
    score: float
    missing_required: list[str] = Field(default_factory=list)
    contributing_evidence: list[Evidence] = Field(default_factory=list)


@dataclass(frozen=True)
class CompiledStrategy:
    """The compiled rule graph for one strategy. Immutable — built once by
    :func:`compile_strategy` at load time and reused for every evaluation."""

    name: str
    required: frozenset[str]
    optional: frozenset[str]
    minimum_score: float
    #: lowercased evidence ``source`` -> policy name
    repeat_policy: dict[str, str] = field(default_factory=dict)

    def evaluate(self, active_evidence: list[Evidence]) -> StrategyEvaluation:
        """Evaluate this strategy against a symbol's current deduped/fresh
        evidence snapshot (as supplied by the Evidence Aggregator). Pure
        function of its inputs — no hidden state, safe to call as often as
        new evidence arrives."""
        filtered = [e for e in active_evidence if _passes_repeat_policy(e, self.repeat_policy)]
        present: dict[str, Evidence] = {}
        for e in filtered:
            present[e.title.strip().lower()] = e  # last one wins if titles somehow collide

        missing_required = self.required - present.keys()
        contributing = [
            evidence
            for title, evidence in present.items()
            if title in self.required or title in self.optional
        ]
        total_score = sum(e.score for e in contributing)
        matched = not missing_required and total_score >= self.minimum_score

        return StrategyEvaluation(
            strategy=self.name,
            matched=matched,
            score=total_score,
            missing_required=sorted(missing_required),
            contributing_evidence=contributing,
        )


def compile_strategy(definition: StrategyDefinition) -> CompiledStrategy:
    """Builds a :class:`CompiledStrategy` from a parsed YAML definition.
    Called once per strategy at load time (see ``app/strategy/loader.py``)."""
    return CompiledStrategy(
        name=definition.name,
        required=frozenset(t.strip().lower() for t in definition.required),
        optional=frozenset(t.strip().lower() for t in definition.optional),
        minimum_score=definition.minimum_score,
        repeat_policy={src.strip().lower(): policy.strip().lower() for src, policy in definition.repeat_policy.items()},
    )


def _passes_repeat_policy(evidence: Evidence, repeat_policy: dict[str, str]) -> bool:
    """Generic, indicator-agnostic filter for evidence-source repeat
    handling. Any evidence producer can opt into this by including
    ``is_first_in_sequence`` (and optionally ``is_first_ever``) in its
    ``Evidence.metadata`` — the Strategy Engine doesn't know or care which
    plugin populated them; it's a documented metadata convention, not a
    special case for Donchian (see ``docs/PLUGIN_GUIDE.md``).

    - ``every_breakout`` (default when a source has no override): every
      occurrence counts.
    - ``first_breakout``: only occurrences flagged as the first in their
      current sequence count (``metadata["is_first_in_sequence"]`` truthy).
    - ``after_pullback``: like ``first_breakout``, but additionally
      excludes the very first occurrence ever seen for that symbol/source
      (``metadata["is_first_ever"]`` truthy) — there has to have been a
      real prior sequence that ended (a pullback) for this to count, not
      just a cold start with no history yet.

    If evidence doesn't carry the sequence metadata at all, this fails
    open (returns True) — most evidence has no concept of "sequence", and
    a repeat_policy override for a source that doesn't emit this metadata
    should never silently exclude everything.
    """
    policy = repeat_policy.get((evidence.source or "").strip().lower())
    if not policy or policy not in _REPEAT_POLICIES or policy == "every_breakout":
        return True

    is_first_in_sequence = evidence.metadata.get("is_first_in_sequence")
    if is_first_in_sequence is None:
        return True  # this evidence type doesn't carry sequence metadata -> fail open

    if policy == "first_breakout":
        return bool(is_first_in_sequence)

    if policy == "after_pullback":
        is_first_ever = bool(evidence.metadata.get("is_first_ever", False))
        return bool(is_first_in_sequence) and not is_first_ever

    return True  # unreachable given the _REPEAT_POLICIES check above, but fail open regardless
