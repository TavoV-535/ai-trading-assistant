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
    """A single price update. ``price`` is the only required field (a tick
    or last-trade price is enough for tick-based indicators like EMA/SMA).

    ``open``/``high``/``low``/``close`` are optional bar (candle) fields for
    indicators that need a real trading range (ATR, ADX, Supertrend,
    Ichimoku, Donchian, ...). When they're omitted — e.g. a raw tick feed —
    indicator plugins that need them fall back to treating the tick as a
    degenerate bar where open == high == low == close == price. A future
    market-data-feed plugin that aggregates real candles can populate all
    four without any change to this schema or to the indicators that
    consume it.
    """

    symbol: str
    price: float
    volume: int | None = None
    timeframe: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None


class PriceMoved(Event):
    symbol: str
    price: float
    change_percent: float
    direction: Literal["up", "down"]


class IndicatorCalculated(Event):
    symbol: str
    indicator: str
    #: A single number for simple indicators (EMA's fast/slow aside — see
    #: below), or a dict for multi-line indicators (MACD's line/signal/
    #: histogram, Bollinger's upper/mid/lower, Ichimoku's four lines,
    #: Supertrend's value+direction, ...). ``Any`` rather than ``float``
    #: because Supertrend's direction is a string ("up"/"down"), not a
    #: number — every other indicator's dict values happen to be floats,
    #: but the schema shouldn't assume that stays true forever.
    value: float | dict[str, Any]
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


class MacroEventOccurred(Event):
    """A normalized macro/economic-calendar event — Fed announcements, CPI
    releases, jobs reports, treasury auctions, government events, and so
    on. One event type covers all of them (rather than a separate schema
    per macro source) because every External Intelligence Platform macro
    plugin describes the same shape: what happened, an optional
    market-wide-vs-symbol-specific scope, and a free-form
    ``macro_event_type`` plugins and the Market Context Engine both key
    off of (e.g. ``"fed_meeting"``, ``"cpi_release"``, ``"jobs_report"``)
    — named ``macro_event_type`` rather than ``event_type`` to avoid
    shadowing :attr:`Event.event_type`, the base class's own routing
    property. See ``app/context/engine.py``'s macro-event promotion for
    how a ``context_hint`` in ``metadata`` becomes a
    ``MarketContextUpdated`` label like "Fed Week" or "CPI Day"."""

    macro_event_type: str
    title: str
    symbol: str | None = None  # None == market-wide, e.g. a Fed announcement
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    so plugins publish it exactly like anything else — the Evidence
    Aggregator subscribes to this the same way any other plugin subscribes
    to ``MarketDataUpdated``."""

    evidence: Evidence


class WeightedEvidenceEvent(BaseModel):
    """The Confidence Weighting Framework's per-evidence output, as carried
    on ``EvidenceAggregated``/``AggregateSnapshot``. See
    ``app/aggregation/weighting.py`` for how ``weight`` is computed —
    ``breakdown`` keeps every named factor visible for transparency rather
    than collapsing straight to one opaque number."""

    model_config = ConfigDict(frozen=True)

    evidence: Evidence
    weight: float = Field(ge=0.0, le=1.0)
    breakdown: dict[str, float] = Field(default_factory=dict)


class EvidenceAggregated(Event):
    """Published by the Evidence Aggregator (`app/aggregation/`) every time
    it processes an ``EvidenceProduced`` event — this is the single
    interface both the Strategy Engine and the Reasoning Engine consume
    instead of subscribing to raw ``EvidenceProduced`` directly.

    ``evidence`` is the newly-arrived, unmodified evidence. ``enrichment``
    carries aggregation metadata about it (occurrence count, freshness,
    whether it's a duplicate confirmation, ...). ``active_evidence`` is the
    deduped, currently-fresh snapshot for this symbol at the moment this
    event was published — nothing in the original event stream is
    discarded (the aggregator's full history is queryable separately), this
    is just the "current picture" downstream systems reason over.
    """

    symbol: str
    evidence: Evidence
    enrichment: dict[str, Any] = Field(default_factory=dict)
    active_evidence: list[Evidence] = Field(default_factory=list)
    has_conflict: bool = False
    #: Parallel view of ``active_evidence`` carrying the Confidence
    #: Weighting Framework's normalized weight + explainable breakdown for
    #: each item (see ``app/aggregation/weighting.py``). Always the same
    #: length/order as ``active_evidence`` — the original Evidence objects
    #: are never replaced, only annotated alongside.
    weighted_evidence: list[WeightedEvidenceEvent] = Field(default_factory=list)


class MarketContextUpdated(Event):
    """Published by the Market Context Engine (``app/context/engine.py``)
    whenever a higher-level market-environment label changes — not raw
    evidence, a *regime* (Bull Trend, High Volatility, Fed Week, ...).

    ``symbol`` is ``None`` for market-wide context (Risk-On/Risk-Off, Fed
    Week, CPI Day, Holiday Session, ...) and set for symbol-specific
    context (Bull/Bear Trend, Gap Day, Trend Exhaustion, Low Liquidity,
    ...). ``context_type`` buckets the label into a family (``"trend"``,
    ``"volatility"``, ``"gap"``, ``"exhaustion"``, ``"liquidity"``,
    ``"macro_event"``, ``"risk_regime"``) so consumers can look up "the
    current trend context for NVDA" without string-matching every label.
    Edge-triggered: the engine only publishes when a label actually
    changes for a given ``(symbol, context_type)``, never every tick."""

    symbol: str | None = None
    context_type: str
    label: str
    confidence: float = Field(default=100.0, ge=0.0, le=100.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


EVENT_TYPES: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in (
        MarketDataUpdated,
        PriceMoved,
        IndicatorCalculated,
        NewsReceived,
        EarningsReleased,
        MacroEventOccurred,
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
        EvidenceAggregated,
        MarketContextUpdated,
        CommandInvoked,
        CommandFailed,
    )
}
