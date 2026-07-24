"""
Data shapes owned by the Portfolio Intelligence Layer. See
``app/portfolio/engine.py`` for the logic that builds and maintains these.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SymbolProfile(BaseModel):
    """The Portfolio Intelligence Layer's evolving, continuously-updated
    intelligence profile for one watchlist symbol — technical evidence,
    external intelligence freshness, market context, a confidence trend,
    matched strategies, and historical alert state, all folded into one
    transparent ``priority_score``. Fetchable directly via
    ``PortfolioIntelligenceEngine.snapshot(symbol)`` /
    ``.ranked_watchlist()`` without waiting for the next event, the same
    on-demand-query convention ``EvidenceAggregator.snapshot()`` and
    ``MarketContextEngine.snapshot()`` already established.
    """

    symbol: str

    # -- technical + fundamental evidence, from EvidenceAggregated -------
    active_evidence_count: int = 0
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    #: Highest Confidence Weighting Framework weight among this symbol's
    #: currently active evidence.
    top_weight: float = 0.0
    avg_weight: float = 0.0
    has_fundamental_evidence: bool = False
    #: Whether that fundamental evidence arrived within
    #: ``portfolio.fundamental_freshness_seconds``.
    fundamental_evidence_fresh: bool = False

    # -- market context, from MarketContextUpdated ------------------------
    context: dict[str, str] = Field(default_factory=dict)

    # -- strategy matches, from StrategyMatched ---------------------------
    matched_strategies: list[str] = Field(default_factory=list)

    # -- confidence trend, derived from a rolling avg_weight history ------
    confidence_trend: str = "unknown"  # "rising" | "falling" | "stable" | "unknown"

    # -- historical alert state, from AlertGenerated -----------------------
    last_alert_at: datetime | None = None
    alert_count: int = 0

    # -- computed ranking ---------------------------------------------------
    priority_score: float = 0.0
    priority_breakdown: dict[str, float] = Field(default_factory=dict)

    updated_at: datetime = Field(default_factory=_utcnow)
