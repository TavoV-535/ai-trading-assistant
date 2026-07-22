"""
Core event schemas.

Everything in the platform communicates by publishing and subscribing to
these events on the :class:`~app.event_bus.bus.EventBus`. Nothing calls
another plugin directly.

Events are immutable (``frozen=True``) — once published, a fact about what
happened doesn't change. If you need a correction, publish a new event.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.evidence.schema import Evidence


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(BaseModel):
    """Base class for every event on the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=_utcnow)
    source: str | None = Field(default=None, description="Plugin or module that published this event")
    correlation_id: UUID | None = Field(
        default=None, description="Links related events together (e.g. a trade's full lifecycle)"
    )

    @property
    def event_type(self) -> str:
        """The event's class name — used for routing on the bus."""
        return type(self).__name__


# ---------------------------------------------------------------- market data


class MarketDataUpdated(Event):
    symbol: str
    price: float
    volume: int | None = None
    timeframe: str | None = None


class PriceMoved(Event):
    symbol: str
    price: float
    change_percent: float
    direction: Literal["up", "down"]


class IndicatorCalculated(Event):
    symbol: str
    indicator: str
    value: float | dict[str, float]
    timeframe: str | None = None


# ---------------------------------------------------------------- news / earnings


class NewsReceived(Event):
    headline: str
    symbol: str | None = None
    url: str | None = None
    provider: str | None = None
    sentiment: Literal["bullish", "bearish", "neutral"] | None = None


class EarningsReleased(Event):
    symbol: str
    eps_actual: float | None = None
    eps_estimate: float | None = None
    revenue_actual: float | None = None
    revenue_estimate: float | None = None
    surprise_percent: float | None = None


# ---------------------------------------------------------------- trading lifecycle


class TradeOpened(Event):
    symbol: str
    side: Literal["long", "short"]
    quantity: float
    entry_price: float
    strategy: str | None = None
    trade_id: UUID = Field(default_factory=uuid4)


class TradeClosed(Event):
    symbol: str
    exit_price: float
    trade_id: UUID
    pnl: float | None = None


class PositionUpdated(Event):
    symbol: str
    quantity: float
    average_price: float
    unrealized_pnl: float | None = None


# ---------------------------------------------------------------- watchlists / strategies


class WatchlistTriggered(Event):
    watchlist: str
    symbol: str
    reason: str | None = None


class StrategyMatched(Event):
    strategy: str
    symbol: str
    score: float
    evidence_count: int = 0


class BacktestFinished(Event):
    strategy: str
    win_rate: float | None = None
    profit_factor: float | None = None
    sharpe: float | None = None
    total_trades: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- journaling / summaries


class JournalCreated(Event):
    trade_id: UUID | None = None
    symbol: str | None = None
    note: str | None = None


class DailySummary(Event):
    summary: str
    pnl: float | None = None
    trade_count: int | None = None


class RiskWarning(Event):
    rule: str
    message: str
    severity: Literal["info", "warning", "critical"] = "warning"


# ---------------------------------------------------------------- commands (Discord)


class CommandInvoked(Event):
    """Published every time a Discord command runs — this is what makes
    'everything logged' true for commands, independent of whichever plugin
    handled it."""

    command: str
    user_id: str
    guild_id: str | None = None
    channel_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class CommandFailed(Event):
    command: str
    user_id: str
    error: str


# ---------------------------------------------------------------- reasoning input


class EvidenceProduced(Event):
    """Wraps a single :class:`~app.evidence.schema.Evidence` object as an event
    so plugins publish it exactly like anything else — the Reasoning Engine
    subscribes to this the same way any other plugin subscribes to
    ``MarketDataUpdated``."""

    evidence: Evidence


EVENT_TYPES: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in (
        MarketDataUpdated,
        PriceMoved,
        IndicatorCalculated,
        NewsReceived,
        EarningsReleased,
        TradeOpened,
        TradeClosed,
        PositionUpdated,
        WatchlistTriggered,
        StrategyMatched,
        BacktestFinished,
        JournalCreated,
        DailySummary,
        RiskWarning,
        EvidenceProduced,
        CommandInvoked,
        CommandFailed,
    )
}
