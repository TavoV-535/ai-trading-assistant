"""Data shapes for the Trading Knowledge Graph. See
``app/knowledge_graph/graph.py`` for the logic that builds these."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

#: The fourteen node types PROJECT.md's Milestone 12 spec asks the graph to
#: model relationships between. Lives here, not scattered as string
#: literals, the same convention ``RISK_TYPES``/``PATTERN_TYPES`` establish
#: for their respective vocabularies.
NODE_TYPES: tuple[str, ...] = (
    "strategy",
    "evidence",
    "indicator",
    "market_context",
    "market_regime",
    "risk_profile",
    "reflection",
    "journal_entry",
    "decision",
    "simulation",
    "outcome",
    "symbol",
    "watchlist",
    "external_intelligence",
)


class NodeType:
    """String constants for :data:`NODE_TYPES` — the same
    "class of constants" convention ``PluginPermission`` establishes,
    so callers write ``NodeType.STRATEGY`` instead of a bare string."""

    STRATEGY = "strategy"
    EVIDENCE = "evidence"
    INDICATOR = "indicator"
    MARKET_CONTEXT = "market_context"
    MARKET_REGIME = "market_regime"
    RISK_PROFILE = "risk_profile"
    REFLECTION = "reflection"
    JOURNAL_ENTRY = "journal_entry"
    DECISION = "decision"
    SIMULATION = "simulation"
    OUTCOME = "outcome"
    SYMBOL = "symbol"
    WATCHLIST = "watchlist"
    EXTERNAL_INTELLIGENCE = "external_intelligence"


class Node(BaseModel):
    """One entity in the graph. ``node_id`` is deterministic
    (``f"{node_type}:{key}"``) so the same real-world entity (e.g. the
    "Momentum Breakout" strategy, or the "NVDA" symbol) always upserts the
    same node rather than creating duplicates — the graph has no separate
    "merge" step because it never needs one."""

    node_id: str
    node_type: str
    label: str
    attributes: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": False}


class Edge(BaseModel):
    """One directed, typed relationship between two nodes. ``relation`` is
    a short verb phrase (``"matched_strategy"``, ``"resulted_in"``,
    ``"occurred_during"``, ...) — always human-readable, since the whole
    point of this graph is that its answers are explainable by tracing
    these exact relations, never an opaque weight or embedding."""

    source_id: str
    target_id: str
    relation: str
    attributes: dict[str, Any] = Field(default_factory=dict)
