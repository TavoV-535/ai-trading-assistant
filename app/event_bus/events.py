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
    """Published by the Capital Protection Engine (``app/capital_protection/``,
    Milestone 11), synthesized from a ``DecisionRecorded`` it observes on
    the bus — the closest existing concept to "a trade" opening, since no
    real broker/paper-trading execution system exists yet (the same
    honest scope decision Milestones 9-10 already made for "a completed
    trade" = a resolved ``DecisionRecorded``). ``quantity``/``entry_price``
    are synthetic, sized against the active Risk Profile's
    ``max_position_size_pct`` and the decision's own confidence — never a
    real broker fill. ``decision_event_id`` links back to the triggering
    ``DecisionRecorded`` for traceability."""

    symbol: str
    side: Literal["long", "short"]
    quantity: float
    entry_price: float
    strategy: str | None = None
    trade_id: UUID = Field(default_factory=uuid4)
    decision_event_id: UUID | None = None


class TradeClosed(Event):
    """The other half of ``TradeOpened`` — published by the Capital
    Protection Engine in the same handler call when the triggering
    ``DecisionRecorded`` already carries a resolved outcome (the common
    case, since the Simulation Engine only ever publishes a decision once
    fully resolved — see ``app/simulation/engine.py``'s module docstring).
    A decision that's still ``outcome_pending`` at the time it's observed
    (a force-flushed, still-unresolved decision at simulation run end)
    gets a ``TradeOpened`` with no matching ``TradeClosed`` — genuinely
    still-open exposure, not fabricated."""

    symbol: str
    exit_price: float
    trade_id: UUID
    pnl: float | None = None
    decision_event_id: UUID | None = None


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
    """Published by the Trading Journal (``app/journal/``) whenever a user
    adds a note or a screenshot placeholder to a journal entry — the
    durable record of *manual* journal activity, complementing
    ``DecisionRecorded`` (automatic, from a simulation) and
    ``ReflectionGenerated`` (automatic, from the Reflection Engine).

    ``decision_event_id`` links this note to the ``DecisionRecorded`` it
    enriches, when the note is about a specific recorded decision rather
    than a symbol generally (``None`` is a valid, honest "general note").
    ``trade_id`` is a deliberate placeholder for a future broker execution
    system (Milestone 10's spec explicitly asks the Journal to be capable
    of combining "future broker execution data" without that system
    existing yet) — always ``None`` today."""

    symbol: str | None = None
    decision_event_id: UUID | None = None
    trade_id: UUID | None = None
    note: str | None = None
    author: str | None = None
    #: Placeholder support only, per the Milestone 10 spec — a URL or path
    #: string, never actual image bytes/upload handling (that's a real,
    #: separate future capability, not faked here).
    screenshot_url: str | None = None


class DailySummary(Event):
    summary: str
    pnl: float | None = None
    trade_count: int | None = None


#: The Capital Protection Engine's canonical risk vocabulary
#: (``app/capital_protection/``, Milestone 11) — every ``RiskEvent.risk_type``
#: is one of these twelve strings, per PROJECT.md's Milestone 11 spec. Lives
#: here, not in the engine module, for the same reason ``ACTION_DIRECTIONS``
#: does: it's part of ``RiskEvent``'s own wire vocabulary, not an
#: implementation detail private to whichever engine happens to evaluate it.
RISK_TYPES: tuple[str, ...] = (
    "daily_drawdown",
    "total_drawdown",
    "trailing_drawdown",
    "consecutive_losses",
    "open_portfolio_risk",
    "position_concentration",
    "sector_concentration",
    "symbol_concentration",
    "correlated_exposure",
    "margin_utilization",
    "broker_constraints",
    "prop_firm_compliance",
)


class RiskEvent(Event):
    """Published by the Capital Protection Engine (``app/capital_protection/``,
    Milestone 11) — one structured, continuously-evolving evaluation of a
    single risk dimension (``risk_type``, one of ``RISK_TYPES``) against
    the currently active Risk Profile. Repurposed from ``RiskWarning``, an
    unused Milestone 1 scaffolding event never referenced anywhere in the
    codebase — the same "reuse the intended vocabulary slot rather than
    invent a new event name" decision Milestone 10 made for
    ``JournalCreated``.

    Per the Milestone 11 spec, the Capital Protection Engine "should never
    directly block trades or commands" — this event is the *only* thing it
    ever does: publish a structured fact so that Discord, the Trading
    Journal, Portfolio Intelligence, the Reflection Engine, and a future AI
    Coach can each independently decide what (if anything) to do about it,
    purely through the Event Bus. Every evaluation is published, not just
    breaches — ``severity="info"`` is itself meaningful "still healthy"
    state, matching the spec's "risk should be modeled as continuously
    evolving state rather than simple threshold checks."

    ``symbol`` is ``None`` for portfolio-wide risk types (drawdown,
    consecutive losses, open portfolio risk, prop firm compliance) and set
    for symbol-scoped ones (position/symbol concentration). ``applicable``
    is ``False`` only for ``margin_utilization``/``broker_constraints`` —
    an honest, always-``False``-today placeholder for a future broker
    integration (the same pattern as ``JournalEntry.broker_execution``),
    never fabricated. ``threshold`` is the active profile's limit for this
    risk type (``None`` for the two inapplicable placeholders).
    ``context`` carries risk-type-specific supporting detail (e.g. the
    correlated symbol pair and its coefficient for
    ``correlated_exposure``) without needing a different event shape per
    risk type."""

    risk_type: str
    symbol: str | None = None
    severity: Literal["info", "warning", "critical"] = "info"
    value: float = 0.0
    threshold: float | None = None
    applicable: bool = True
    profile_name: str = ""
    message: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


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


class SymbolProfileUpdated(Event):
    """Published by the Portfolio Intelligence Layer
    (``app/portfolio/engine.py``) whenever a tracked watchlist symbol's
    evolving intelligence profile changes materially — not on every tick,
    edge-triggered like everything else in this codebase. ``priority_score``
    is what ``/watchlist`` ranks by; ``priority_breakdown`` keeps every
    contributing factor visible for transparency, the same explainability
    convention the Confidence Weighting Framework established
    (``app/aggregation/weighting.py``)."""

    symbol: str
    priority_score: float = Field(ge=0.0, le=100.0)
    priority_breakdown: dict[str, float] = Field(default_factory=dict)
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    top_weight: float = 0.0
    confidence_trend: str = "unknown"  # "rising" | "falling" | "stable" | "unknown"
    context: dict[str, str] = Field(default_factory=dict)
    matched_strategies: list[str] = Field(default_factory=list)
    last_alert_at: datetime | None = None
    alert_count: int = 0


class AlertGenerated(Event):
    """Published by the Event Prioritization Engine
    (``app/prioritization/engine.py``) when a candidate development (fresh
    evidence, a strategy match, a market context shift) clears the alert
    threshold and isn't suppressed as a recent duplicate. This is the one
    event type that's meant to actually reach the user — ``app/discord/
    bot.py`` subscribes to it and posts to ``discord.alert_channel_id`` if
    configured (graceful no-op, logged, if it isn't).

    ``symbol`` is ``None`` for a market-wide alert (e.g. a Risk-Off regime
    shift). ``breakdown`` mirrors every other explainability convention in
    this codebase — the score is never an opaque number."""

    symbol: str | None = None
    title: str
    message: str
    score: float = Field(ge=0.0, le=100.0)
    breakdown: dict[str, float] = Field(default_factory=dict)
    reason: str
    urgency: str = "normal"  # "low" | "normal" | "high" | "critical"
    source_event_type: str


class DecisionRecorded(Event):
    """Published by the Decision Timeline (``app/timeline/``) — the
    canonical historical record of one point-in-time reasoning snapshot,
    produced today only by the Simulation Engine (``app/simulation/``)
    replaying historical data, one per watched symbol every
    ``simulation.decision_interval_bars`` simulated bars. Not a trade
    order and not a recommendation — ``simulated_action`` is deliberately
    phrased as a hypothesis label ("watch_bullish"/"watch_bearish"/
    "watch_neutral"/"no_action"), never "buy"/"sell", matching this
    platform's explicit non-goal of being a signal-selling bot.

    Everything here is built from the exact same query surface ``/analyze``
    already uses (``EvidenceAggregator.snapshot()``,
    ``MarketContextEngine.snapshot()``, ``ReasoningEngine.analyze()``,
    ``PortfolioIntelligenceEngine.snapshot()``) — the Decision Timeline
    adds no parallel reasoning path of its own, only a durable record of
    what those systems already said at the time.

    ``outcome`` starts unset. The Simulation Engine resolves it once
    ``lookahead_bars`` further simulated bars of price data exist for the
    symbol (comparing subsequent price action against the decision's
    implied direction) and only then publishes this event — an
    already-published event is never mutated (events are immutable), so a
    decision is recorded exactly once, fully resolved, or — if the
    simulation run ends before enough lookahead data exists — with
    ``outcome=None`` and ``outcome_pending=True``, an honest "not enough
    data yet" rather than a fabricated result."""

    symbol: str
    #: Market-wide + symbol-specific labels from the Market Context Engine,
    #: symbol-specific winning on any collision (same convention as
    #: ``ReasoningEngine.context_for()``).
    market_context: dict[str, str] = Field(default_factory=dict)
    #: Human-readable summaries of the active technical evidence
    #: (``f"{source}: {title} ({direction}, {confidence}/100)"``), split
    #: from fundamental evidence via ``Evidence.category`` /
    #: ``FUNDAMENTAL_CATEGORIES`` (see ``app/evidence/schema.py``).
    technical_evidence: list[str] = Field(default_factory=list)
    fundamental_evidence: list[str] = Field(default_factory=list)
    #: ``f"{source}:{title}"`` -> the Confidence Weighting Framework's
    #: normalized ``[0, 1]`` weight for that evidence item at decision time.
    confidence_weights: dict[str, float] = Field(default_factory=dict)
    #: Currently matched strategy names (from the Portfolio Intelligence
    #: Layer's tracked profile when the symbol is on the watchlist, empty
    #: otherwise).
    strategy_matches: list[str] = Field(default_factory=list)
    #: ``ReasoningOutput.market_summary`` — the exact text ``/analyze``
    #: would show, not a re-derived paraphrase.
    reasoning_summary: str = ""
    #: ``ReasoningOutput.source`` — "ai" | "evidence_only" |
    #: "insufficient_evidence". Simulation runs always use "evidence_only"
    #: (a real AI provider is deliberately never called during simulation —
    #: non-deterministic, costs real API calls, and unnecessary for
    #: reproducible historical analysis).
    reasoning_source: str = "insufficient_evidence"
    confidence: float = Field(default=0.0, ge=0.0, le=100.0)
    simulated_action: str = "no_action"  # "watch_bullish" | "watch_bearish" | "watch_neutral" | "no_action"
    price_at_decision: float | None = None
    bar_index: int = 0
    lookahead_bars: int = 0
    #: "correct" | "incorrect" | "neutral" | None (still pending / not
    #: applicable to a "no_action" decision).
    outcome: str | None = None
    outcome_price_change_pct: float | None = None
    outcome_pending: bool = True


#: ``DecisionRecorded.simulated_action`` -> the direction it implies, for
#: outcome resolution (``app/simulation/engine.py``) and for splitting a
#: decision's evidence into "supporting" vs. "contradictory" (the
#: Reflection Engine, ``app/reflection/engine.py``). Lives here, not in
#: either consumer's module, because it's fundamentally part of
#: ``DecisionRecorded``'s own vocabulary — what a ``simulated_action``
#: value *means* — not implementation detail private to whichever engine
#: happens to read it first. ``"no_action"`` deliberately maps to nothing
#: (absent from this dict) — there's no directional claim to compare
#: anything against.
ACTION_DIRECTIONS: dict[str, str] = {
    "watch_bullish": "bullish",
    "watch_bearish": "bearish",
    "watch_neutral": "neutral",
}


class ReflectionGenerated(Event):
    """Published by the Reflection Engine (``app/reflection/``) — a
    structured post-decision analysis generated automatically the moment
    a ``DecisionRecorded``'s outcome resolves (``outcome_pending`` flips to
    ``False``), whether that's a real directional correct/incorrect/
    neutral verdict or an honest "no evidence existed" for a ``no_action``
    decision. Exactly one reflection per resolved decision.

    Built entirely from the ``DecisionRecorded`` event that triggered it
    plus this engine's own cached ``SymbolProfileUpdated.confidence_trend``
    (the same cache-only pattern the Event Prioritization Engine already
    uses — see ``app/prioritization/engine.py``) — never by reaching into
    the Evidence Aggregator, Reasoning Engine, or Portfolio Intelligence
    Layer directly. This is what lets the Journal, a future AI Coach,
    Performance Analytics, and a future Dashboard all consume reflections
    independently, purely through the Event Bus, with no direct dependency
    on the Reflection Engine's internals.

    Generation is deterministic and rule-based by default (no AI provider
    call) — the same "graceful, honest, zero-external-dependency default"
    convention the Reasoning Engine's ``evidence_only`` mode and the
    Simulation Engine's ``provider=None`` already establish."""

    symbol: str
    #: The ``DecisionRecorded`` this reflection is about — how the Journal
    #: (``app/journal/``) attaches a reflection to the right entry without
    #: the Reflection Engine needing to know the Journal exists.
    decision_event_id: UUID
    #: Echoes ``DecisionRecorded.reasoning_summary`` — "why the decision
    #: was made," in the reasoning system's own words, not re-derived.
    reasoning: str = ""
    #: Technical/fundamental evidence lines (same formatted-string
    #: convention as ``DecisionRecorded.technical_evidence`` — see
    #: ``app/evidence/formatting.py``) whose parsed direction agrees with
    #: ``simulated_action``'s implied direction.
    supporting_evidence: list[str] = Field(default_factory=list)
    #: The same, but whose direction disagrees — the case *against* the
    #: decision that was made anyway, made visible rather than hidden.
    contradictory_evidence: list[str] = Field(default_factory=list)
    market_context: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=100.0)
    #: This symbol's Portfolio Intelligence Layer confidence trend
    #: ("rising"/"falling"/"stable"/"unknown") at the moment this
    #: reflection was generated — cached from ``SymbolProfileUpdated``,
    #: never queried live.
    confidence_evolution: str = "unknown"
    simulated_action: str = "no_action"
    outcome: str | None = None
    outcome_price_change_pct: float | None = None
    #: A short, deterministic, rule-based takeaway — never a hindsight
    #: trading directive ("should have bought") — framed as an honest
    #: observation about the evidence and outcome.
    lessons_learned: str = ""
    #: A short, deterministic, rule-based suggestion for what could sharpen
    #: future analysis of similar setups — e.g. "the contradictory OBV
    #: signal was outweighed; consider whether volume-based evidence
    #: deserves more weight in Bear Trend regimes." Always a hypothesis
    #: to consider, never a directive.
    potential_improvements: str = ""


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
        RiskEvent,
        EvidenceProduced,
        EvidenceAggregated,
        MarketContextUpdated,
        SymbolProfileUpdated,
        AlertGenerated,
        DecisionRecorded,
        ReflectionGenerated,
        CommandInvoked,
        CommandFailed,
    )
}
