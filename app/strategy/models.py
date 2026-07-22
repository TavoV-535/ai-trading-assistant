"""
The declarative strategy schema. A strategy is pure data — a YAML file, not
a Python plugin — parsed into a :class:`StrategyDefinition` and then
compiled once (see ``app/strategy/compiler.py``) into a fast, reusable
rule graph. Nothing in this module (or anywhere in ``app/strategy/``)
imports or references a specific indicator's implementation — a strategy
only ever names evidence by its ``title`` (and, for repeat handling, its
``source``), exactly as those appear on a published ``Evidence`` object.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class StrategyDefinition(BaseModel):
    """Schema for ``plugins/strategies/<name>/strategy.yaml``.

    Example::

        name: Momentum Breakout
        required:
          - Bullish EMA Cross
          - Donchian Channel Breakout (New High)
        optional:
          - Bullish SMA Cross
          - CCI Breakout Above 100
        minimum_score: 32
        repeat_policy:
          Donchian: after_pullback
    """

    name: str
    #: Evidence titles that must ALL be present (fresh) for this strategy
    #: to be eligible to match at all.
    required: list[str] = Field(default_factory=list)
    #: Evidence titles that aren't required, but whose score counts toward
    #: `minimum_score` when present.
    optional: list[str] = Field(default_factory=list)
    #: Sum of scores from present required + optional evidence must reach
    #: this for the strategy to actually match.
    minimum_score: float = 0.0
    #: Per-evidence-source repeat-handling override — see
    #: ``app/strategy/compiler.py``'s repeat-policy filter for the exact
    #: semantics of "every_breakout" / "first_breakout" / "after_pullback".
    #: Keyed by the evidence's ``source`` field (e.g. "Donchian"), not by
    #: title, since sequence metadata is a per-source concept.
    repeat_policy: dict[str, str] = Field(default_factory=dict)
