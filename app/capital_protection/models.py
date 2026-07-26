"""Data shapes produced by the Capital Protection Engine. See
``app/capital_protection/engine.py`` for the logic that builds these."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.event_bus.events import RiskEvent


class CapitalProtectionStatus(BaseModel):
    """One point-in-time snapshot of the Capital Protection Engine's
    continuously-evolving state — the query-facing shape ``/risk`` (and
    any other on-demand consumer) reads, rather than re-deriving anything
    from raw event history itself. ``latest_risk_events`` holds the most
    recently published :class:`~app.event_bus.events.RiskEvent` per risk
    type (and, for symbol-scoped types, per ``"{risk_type}:{symbol}"``)
    — the same "latest snapshot per key" convention
    ``PortfolioIntelligenceEngine.snapshot()`` already establishes."""

    enabled: bool
    active_profile: str
    equity: float
    peak_equity: float
    daily_drawdown_pct: float
    total_drawdown_pct: float
    trailing_drawdown_pct: float
    consecutive_losses: int
    open_position_count: int
    open_position_notional: float
    latest_risk_events: dict[str, RiskEvent] = Field(default_factory=dict)
    profile_names: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}
