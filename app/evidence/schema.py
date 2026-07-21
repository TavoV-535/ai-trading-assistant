"""
The Universal Evidence Object.

Plugins do NOT return signals. Plugins return evidence. The Reasoning Engine
is the only thing that combines evidence into a conclusion — no plugin
decides trades.

    {
        "source": "EMA",
        "category": "Trend",
        "title": "Bullish EMA Cross",
        "score": 15,
        "confidence": 91,
        "direction": "bullish",
        "metadata": {"fast": 20, "slow": 50}
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

Direction = Literal["bullish", "bearish", "neutral"]


class EvidenceCategory:
    """Common category labels. Not an enum — plugins may use any string;
    these constants just keep spelling consistent across the codebase."""

    TREND = "Trend"
    MOMENTUM = "Momentum"
    VOLUME = "Volume"
    VOLATILITY = "Volatility"
    NEWS = "News"
    MACRO = "Macro"
    SECTOR = "Sector"
    OPTIONS_FLOW = "Options Flow"
    EARNINGS = "Earnings"
    RISK = "Risk"
    PATTERN = "Pattern"
    HISTORICAL = "Historical Patterns"


class Evidence(BaseModel):
    """A single, self-contained observation from one plugin.

    Immutable once created. The Reasoning Engine gathers many of these
    (from Trend, Momentum, News, Macro, Risk, Historical Patterns, ...) and
    synthesizes them into a thesis — it never trusts a single piece of
    evidence to make a decision on its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID = Field(default_factory=uuid4)
    source: str = Field(..., description="Plugin name that produced this evidence, e.g. 'EMA'")
    category: str = Field(..., description="e.g. Trend, Momentum, News, Macro, Risk, Historical Patterns")
    title: str = Field(..., description="Short human-readable summary, e.g. 'Bullish EMA Cross'")
    score: float = Field(..., description="Plugin-defined weight/magnitude — not bounded, comparable within a category")
    confidence: float = Field(..., ge=0, le=100, description="0-100 — how sure the plugin is")
    direction: Direction
    symbol: str | None = None
    timeframe: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("direction", mode="before")
    @classmethod
    def _normalize_direction(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, value: float) -> float:
        return round(value, 2)
