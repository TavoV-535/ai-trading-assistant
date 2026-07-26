"""
The Capital Protection Engine.

Per PROJECT.md's Milestone 11 spec: not a traditional Risk Engine that
blocks trades, but a continuous capital-preservation monitor. It
evaluates twelve risk dimensions (``app.event_bus.events.RISK_TYPES``) —
daily/total/trailing drawdown, consecutive losses, open portfolio risk,
position/sector/symbol concentration, correlated exposure, margin
utilization, broker constraints, and prop firm compliance — against the
currently active :class:`~app.capital_protection.profiles.RiskProfile`,
and publishes exactly one :class:`~app.event_bus.events.RiskEvent` per
evaluation. **It never blocks a trade or a command** — the Event Bus is
the only thing it ever touches; Discord, the Trading Journal, Portfolio
Intelligence, the Reflection Engine, and a future AI Coach each decide
independently what (if anything) to do about a given ``RiskEvent``.

Not a plugin — a core service, the same tier as the Decision Timeline,
Reflection Engine, or Trading Journal. Attach once at bootstrap (live) or
once per Simulation Engine run (``app/simulation/engine.py``) — the exact
same engine class in both cases is what "simulation and live modes using
the same Capital Protection Engine" (this milestone's completion
checklist) means structurally, not just as a claim.

**Where "a trade" comes from.** No real broker/paper-trading execution
system exists yet (the same honest scope boundary Milestones 9-10 already
established), so this engine treats every non-``"no_action"``,
non-neutral ``DecisionRecorded`` it observes as one synthetic trade,
sized against the active profile's ``max_position_size_pct`` scaled by
the decision's own confidence. Because the Simulation Engine only ever
publishes a decision once it's fully resolved (see that module's
docstring — "an already-published event is never mutated"), this engine
synthesizes the standard trade-lifecycle vocabulary
(``TradeOpened``/``TradeClosed``, both previously-unused Milestone 1
scaffolding events) itself, from that one ``DecisionRecorded``: a
``TradeOpened`` is always published; a ``TradeClosed`` follows
immediately, in the same handler call, whenever the triggering decision
already carries a resolved outcome (the common case). A decision that's
still ``outcome_pending`` when observed (a force-flushed, still-unresolved
decision at simulation run end) gets a ``TradeOpened`` with **no**
matching ``TradeClosed`` — genuinely open exposure, not fabricated.

**An honest, documented scope boundary this implies:** because a
``DecisionRecorded`` is (almost always) already resolved by the time this
engine observes it, ``open_portfolio_risk`` (which counts *currently*
open positions) is usually near-zero mid-run and only meaningfully
non-zero for decisions still pending at a run's end — an accurate
reflection of what "open" can honestly mean given today's event
producers, not a bug. ``position_concentration``, ``symbol_concentration``,
and ``sector_concentration`` are therefore computed over a rolling window
of *recently closed* trades instead (``capital_protection.
concentration_window_trades``) — "how concentrated has recent trading
activity been," a real and useful risk lens in its own right, clearly
distinct from literal concurrent open exposure. Once a real
position-open/position-close signal exists (a future broker/paper-trading
integration — see ``JournalEntry.broker_execution``), these three can
switch to genuinely-concurrent open positions with no change to their
public shape.

Risk is modeled as continuously evolving state, not one-shot threshold
checks: a running equity curve (peak, daily-start, and a bounded trailing
window), a consecutive-loss streak counter, a bounded rolling window of
recent trades, and a bounded rolling window of per-symbol prices for a
real (Pearson) correlation calculation — all incrementally updated as
events arrive, never recomputed from scratch. Severity is graduated
(``"info"`` / ``"warning"`` / ``"critical"``), not binary pass/fail: below
70% of a profile's limit is healthy, 70-100% is a warning, at or beyond
100% is a breach — published every evaluation, including the healthy
ones, since "still healthy" is itself meaningful continuously-monitored
state.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from app.capital_protection.models import CapitalProtectionStatus
from app.capital_protection.profiles import RiskProfile, RiskProfileRegistry
from app.core.clock import Clock, SystemClock
from app.event_bus.bus import EventBus
from app.event_bus.events import ACTION_DIRECTIONS, DecisionRecorded, MarketDataUpdated, RiskEvent, TradeClosed, TradeOpened
from app.logging import get_logger

log = get_logger(__name__)

#: Below this fraction of a profile's limit, a risk dimension is healthy
#: ("info"). Between this and 1.0, it's elevated ("warning"). At or beyond
#: 1.0, it's a breach ("critical"). One shared rule for every graduated
#: (non-binary) risk type -- a single source of truth, the same
#: "generic scoring helper" pattern app.prioritization.scoring establishes.
_WARNING_RATIO = 0.7


def _severity_for(ratio: float) -> Literal["info", "warning", "critical"]:
    if ratio >= 1.0:
        return "critical"
    if ratio >= _WARNING_RATIO:
        return "warning"
    return "info"


@dataclass
class _OpenPosition:
    trade_id: UUID
    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    notional: float
    sector: str
    decision_event_id: UUID | None


@dataclass
class _ClosedTrade:
    symbol: str
    side: Literal["long", "short"]
    notional: float
    sector: str
    pnl: float
    closed_at: datetime


class CapitalProtectionEngine:
    """Continuously evaluates capital-preservation risk and publishes
    structured ``RiskEvent``s. Attach once at bootstrap (or once per
    Simulation Engine run — see ``app/simulation/engine.py``); every
    consumer (``/risk`` included) queries ``status()``, the same
    read-only-query pattern every other core engine in this codebase
    exposes."""

    def __init__(self, settings: Any, *, clock: Clock | None = None) -> None:
        section = getattr(settings, "capital_protection", None)
        self._enabled = bool(getattr(section, "enabled", True))
        self._starting_equity = float(getattr(section, "starting_equity", 100_000.0))
        self._correlation_window_bars = max(2, int(getattr(section, "correlation_window_bars", 60)))
        self._correlation_min_samples = max(2, int(getattr(section, "correlation_min_samples", 10)))
        self._trailing_window_trades = max(1, int(getattr(section, "trailing_drawdown_window_trades", 20)))
        self._concentration_window_trades = max(1, int(getattr(section, "concentration_window_trades", 20)))
        self._evaluation_interval_ticks = max(1, int(getattr(section, "evaluation_interval_ticks", 20)))
        self._symbol_sectors: dict[str, str] = dict(getattr(section, "symbol_sectors", None) or {})
        #: Defaults to the real wall clock -- the Simulation Engine injects
        #: a SimulatedClock, exactly like every other clock-injected core
        #: engine (see app/core/clock.py).
        self._clock: Clock = clock or SystemClock()
        self._profiles = RiskProfileRegistry(settings)

        self._equity = self._starting_equity
        self._peak_equity = self._starting_equity
        self._daily_start_equity = self._starting_equity
        #: Established immediately, from the pre-any-trade starting equity
        #: -- never lazily on first evaluation, which would otherwise stamp
        #: "start of day" equity *after* that same evaluation cycle's own
        #: trade already moved it, silently hiding day-one's real daily
        #: drawdown (caught by this module's own test suite -- see
        #: tests/test_capital_protection_engine.py).
        self._daily_date: date = self._clock.now().date()
        self._trailing_equity: "deque[float]" = deque(maxlen=self._trailing_window_trades)
        self._consecutive_losses = 0
        self._open_positions: dict[UUID, _OpenPosition] = {}
        self._recent_closed: "deque[_ClosedTrade]" = deque(maxlen=self._concentration_window_trades)
        self._price_history: dict[str, "deque[float]"] = defaultdict(lambda: deque(maxlen=self._correlation_window_bars))
        self._tick_counter = 0
        #: Latest published RiskEvent per key -- ``risk_type`` for
        #: portfolio-wide types, ``"{risk_type}:{symbol}"`` for
        #: symbol-scoped ones. The query surface ``status()`` exposes.
        self._latest: dict[str, RiskEvent] = {}
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="capital_protection_decisions")
        event_bus.subscribe(MarketDataUpdated, self._on_market_data_updated, name="capital_protection_market_data")
        log.info(
            "capital_protection_engine_attached",
            enabled=self._enabled,
            active_profile=self._profiles.current().name,
            starting_equity=self._starting_equity,
        )

    # ---------------------------------------------------------------- decision-driven

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        if not self._enabled:
            return

        direction = ACTION_DIRECTIONS.get(event.simulated_action)
        if direction is not None and direction != "neutral" and event.price_at_decision and event.price_at_decision > 0:
            await self._open_and_maybe_close_trade(event, direction)

        await self._evaluate_decision_driven_risk(event.correlation_id)

    async def _open_and_maybe_close_trade(self, event: DecisionRecorded, direction: str) -> None:
        side: Literal["long", "short"] = "long" if direction == "bullish" else "short"
        profile = self._profiles.current()
        #: Synthetic sizing: the active profile's own position-size ceiling,
        #: scaled down by the decision's confidence -- a low-confidence
        #: hypothesis never sizes up to the profile's full allowance. A
        #: documented placeholder for a future real position-sizing
        #: algorithm, not a claim of realism.
        confidence_scale = max(0.0, min(1.0, event.confidence / 100.0))
        notional = self._equity * (profile.max_position_size_pct / 100.0) * confidence_scale
        quantity = notional / event.price_at_decision if event.price_at_decision else 0.0
        trade_id = uuid4()
        sector = self._sector_for(event.symbol)

        await self._publish_event(
            TradeOpened(
                source="CapitalProtectionEngine",
                timestamp=self._clock.now(),
                correlation_id=event.correlation_id,
                symbol=event.symbol,
                side=side,
                quantity=quantity,
                entry_price=event.price_at_decision,
                strategy=event.strategy_matches[0] if event.strategy_matches else None,
                trade_id=trade_id,
                decision_event_id=event.event_id,
            )
        )
        self._open_positions[trade_id] = _OpenPosition(
            trade_id=trade_id, symbol=event.symbol, side=side, entry_price=event.price_at_decision,
            notional=notional, sector=sector, decision_event_id=event.event_id,
        )

        if not event.outcome_pending:
            await self._close_trade(trade_id, event, side, notional)

    async def _close_trade(self, trade_id: UUID, event: DecisionRecorded, side: Literal["long", "short"], notional: float) -> None:
        position = self._open_positions.pop(trade_id, None)
        if position is None:
            return

        raw_pct = event.outcome_price_change_pct or 0.0
        signed_pct = raw_pct if side == "long" else -raw_pct
        pnl = notional * (signed_pct / 100.0)
        exit_price = position.entry_price * (1 + raw_pct / 100.0)

        await self._publish_event(
            TradeClosed(
                source="CapitalProtectionEngine",
                timestamp=self._clock.now(),
                correlation_id=event.correlation_id,
                symbol=event.symbol,
                exit_price=exit_price,
                trade_id=trade_id,
                pnl=pnl,
                decision_event_id=event.event_id,
            )
        )

        self._equity += pnl
        self._peak_equity = max(self._peak_equity, self._equity)
        self._trailing_equity.append(self._equity)
        if pnl < 0:
            self._consecutive_losses += 1
        elif pnl > 0:
            self._consecutive_losses = 0
        # pnl == 0 ("neutral" outcome): the streak is neither extended nor
        # reset -- a neutral result is honestly neither a win nor a loss.

        self._recent_closed.append(
            _ClosedTrade(symbol=event.symbol, side=side, notional=notional, sector=position.sector, pnl=pnl, closed_at=self._clock.now())
        )

    async def _evaluate_decision_driven_risk(self, correlation_id: UUID | None) -> None:
        self._maybe_roll_day()
        profile = self._profiles.current()

        daily_dd = self._daily_drawdown_pct()
        await self._publish_risk(
            "daily_drawdown", value=daily_dd, threshold=profile.max_daily_loss_pct,
            message=f"Daily drawdown {daily_dd:.2f}% (limit {profile.max_daily_loss_pct:.2f}%).",
            correlation_id=correlation_id,
        )

        total_dd = self._total_drawdown_pct()
        await self._publish_risk(
            "total_drawdown", value=total_dd, threshold=profile.max_total_drawdown_pct,
            message=f"Total drawdown {total_dd:.2f}% (limit {profile.max_total_drawdown_pct:.2f}%).",
            correlation_id=correlation_id,
        )

        trailing_dd = self._trailing_drawdown_pct()
        await self._publish_risk(
            "trailing_drawdown", value=trailing_dd, threshold=profile.max_total_drawdown_pct,
            message=f"Trailing drawdown {trailing_dd:.2f}% over the last {self._trailing_window_trades} trade(s) "
            f"(limit {profile.max_total_drawdown_pct:.2f}%).",
            context={"window_trades": self._trailing_window_trades},
            correlation_id=correlation_id,
        )

        await self._publish_risk(
            "consecutive_losses", value=float(self._consecutive_losses), threshold=float(profile.max_consecutive_losses),
            message=f"{self._consecutive_losses} consecutive losing trade(s) (limit {profile.max_consecutive_losses}).",
            correlation_id=correlation_id,
        )

        open_pct, open_ctx = self._open_portfolio_risk_pct()
        await self._publish_risk(
            "open_portfolio_risk", value=open_pct, threshold=profile.max_portfolio_exposure_pct,
            message=f"Open portfolio risk {open_pct:.2f}% of equity (limit {profile.max_portfolio_exposure_pct:.2f}%).",
            context=open_ctx, correlation_id=correlation_id,
        )

        pos_pct, pos_ctx = self._position_concentration()
        await self._publish_risk(
            "position_concentration", value=pos_pct, threshold=profile.max_position_size_pct,
            message=f"Largest recent single trade is {pos_pct:.2f}% of recent trading notional "
            f"(limit {profile.max_position_size_pct:.2f}%).",
            symbol=pos_ctx.get("symbol"), context=pos_ctx, correlation_id=correlation_id,
        )

        sym_pct, sym_ctx = self._symbol_concentration()
        await self._publish_risk(
            "symbol_concentration", value=sym_pct, threshold=profile.symbol_limit_pct,
            message=f"Most-traded symbol is {sym_pct:.2f}% of recent trading notional (limit {profile.symbol_limit_pct:.2f}%).",
            symbol=sym_ctx.get("symbol"), context=sym_ctx, correlation_id=correlation_id,
        )

        sec_pct, sec_ctx = self._sector_concentration()
        await self._publish_risk(
            "sector_concentration", value=sec_pct, threshold=profile.sector_limit_pct,
            message=f"Most-traded sector is {sec_pct:.2f}% of recent trading notional (limit {profile.sector_limit_pct:.2f}%).",
            context=sec_ctx, correlation_id=correlation_id,
        )

        ratio, compliance_ctx = self._prop_firm_compliance_ratio(profile, daily_dd, total_dd)
        await self._publish_risk(
            "prop_firm_compliance", value=ratio, threshold=1.0,
            severity="critical" if ratio >= 1.0 else "info",
            message=(
                f"Within prop firm limits under the '{profile.name}' profile."
                if ratio < 1.0
                else f"Breached prop firm limits under the '{profile.name}' profile."
            ),
            context=compliance_ctx, correlation_id=correlation_id,
        )

    # ---------------------------------------------------------------- market-data-driven

    async def _on_market_data_updated(self, event: MarketDataUpdated) -> None:
        if not self._enabled:
            return

        price = event.close if event.close is not None else event.price
        if price and price > 0:
            self._price_history[event.symbol].append(price)

        self._tick_counter += 1
        if self._tick_counter % self._evaluation_interval_ticks != 0:
            return

        self._maybe_roll_day()
        await self._evaluate_market_driven_risk(event.correlation_id)

    async def _evaluate_market_driven_risk(self, correlation_id: UUID | None) -> None:
        profile = self._profiles.current()

        corr, corr_ctx = self._max_correlated_pair()
        await self._publish_risk(
            "correlated_exposure", value=abs(corr), threshold=profile.correlation_limit,
            message=(
                f"Highest pairwise correlation among recently-exposed symbols is {abs(corr):.2f} "
                f"(limit {profile.correlation_limit:.2f})."
                if corr_ctx
                else "No pair of recently-exposed symbols has enough overlapping price history to correlate yet."
            ),
            context=corr_ctx, correlation_id=correlation_id,
        )

        placeholder_message = (
            "No live broker/margin integration exists yet -- always inapplicable, an honest "
            "placeholder for a future capability (see JournalEntry.broker_execution)."
        )
        await self._publish_risk(
            "margin_utilization", value=0.0, threshold=None, applicable=False, severity="info",
            message=placeholder_message, correlation_id=correlation_id,
        )
        await self._publish_risk(
            "broker_constraints", value=0.0, threshold=None, applicable=False, severity="info",
            message=placeholder_message, correlation_id=correlation_id,
        )

    # ---------------------------------------------------------------- equity / drawdown math

    def _maybe_roll_day(self) -> None:
        current_date = self._clock.now().date()
        if current_date != self._daily_date:
            self._daily_date = current_date
            self._daily_start_equity = self._equity

    def _daily_drawdown_pct(self) -> float:
        if self._daily_start_equity <= 0:
            return 0.0
        return max(0.0, (self._daily_start_equity - self._equity) / self._daily_start_equity * 100.0)

    def _total_drawdown_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - self._equity) / self._peak_equity * 100.0)

    def _trailing_drawdown_pct(self) -> float:
        if not self._trailing_equity:
            return 0.0
        peak = max(self._trailing_equity)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - self._equity) / peak * 100.0)

    def _prop_firm_compliance_ratio(self, profile: RiskProfile, daily_dd: float, total_dd: float) -> tuple[float, dict[str, Any]]:
        daily_ratio = daily_dd / profile.max_daily_loss_pct if profile.max_daily_loss_pct > 0 else 0.0
        total_ratio = total_dd / profile.max_total_drawdown_pct if profile.max_total_drawdown_pct > 0 else 0.0
        ratio = max(daily_ratio, total_ratio)
        return ratio, {"daily_drawdown_pct": round(daily_dd, 4), "total_drawdown_pct": round(total_dd, 4)}

    # ---------------------------------------------------------------- exposure / concentration math

    def _sector_for(self, symbol: str) -> str:
        return self._symbol_sectors.get(symbol, "Unknown")

    def _open_portfolio_risk_pct(self) -> tuple[float, dict[str, Any]]:
        positions = list(self._open_positions.values())
        total_notional = sum(p.notional for p in positions)
        pct = (total_notional / self._equity * 100.0) if self._equity > 0 else 0.0
        return pct, {"open_position_count": len(positions), "total_notional": round(total_notional, 2)}

    def _position_concentration(self) -> tuple[float, dict[str, Any]]:
        trades = list(self._recent_closed)
        total = sum(t.notional for t in trades)
        if not trades or total <= 0:
            return 0.0, {}
        largest = max(trades, key=lambda t: t.notional)
        pct = largest.notional / total * 100.0
        return pct, {"symbol": largest.symbol, "notional": round(largest.notional, 2), "window_total_notional": round(total, 2)}

    def _symbol_concentration(self) -> tuple[float, dict[str, Any]]:
        trades = list(self._recent_closed)
        total = sum(t.notional for t in trades)
        if not trades or total <= 0:
            return 0.0, {}
        by_symbol: dict[str, float] = defaultdict(float)
        for t in trades:
            by_symbol[t.symbol] += t.notional
        symbol, notional = max(by_symbol.items(), key=lambda kv: kv[1])
        pct = notional / total * 100.0
        return pct, {"symbol": symbol, "notional": round(notional, 2), "window_total_notional": round(total, 2)}

    def _sector_concentration(self) -> tuple[float, dict[str, Any]]:
        trades = list(self._recent_closed)
        total = sum(t.notional for t in trades)
        if not trades or total <= 0:
            return 0.0, {}
        by_sector: dict[str, float] = defaultdict(float)
        for t in trades:
            by_sector[t.sector] += t.notional
        sector, notional = max(by_sector.items(), key=lambda kv: kv[1])
        pct = notional / total * 100.0
        return pct, {"sector": sector, "notional": round(notional, 2), "window_total_notional": round(total, 2)}

    def _exposed_symbols(self) -> set[str]:
        symbols = {p.symbol for p in self._open_positions.values()}
        symbols |= {t.symbol for t in self._recent_closed}
        return symbols

    def _returns(self, symbol: str) -> list[float]:
        prices = list(self._price_history.get(symbol, ()))
        if len(prices) < 2:
            return []
        return [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices)) if prices[i - 1] != 0]

    @staticmethod
    def _pearson(a: list[float], b: list[float]) -> float | None:
        n = min(len(a), len(b))
        if n < 2:
            return None
        a, b = a[-n:], b[-n:]
        mean_a, mean_b = sum(a) / n, sum(b) / n
        cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
        var_a = sum((x - mean_a) ** 2 for x in a)
        var_b = sum((y - mean_b) ** 2 for y in b)
        if var_a <= 0 or var_b <= 0:
            return None
        return cov / ((var_a**0.5) * (var_b**0.5))

    def _max_correlated_pair(self) -> tuple[float, dict[str, Any]]:
        symbols = sorted(self._exposed_symbols())
        best = 0.0
        best_pair: tuple[str, str] | None = None
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                ra, rb = self._returns(symbols[i]), self._returns(symbols[j])
                if len(ra) < self._correlation_min_samples or len(rb) < self._correlation_min_samples:
                    continue
                corr = self._pearson(ra, rb)
                if corr is not None and abs(corr) > abs(best):
                    best, best_pair = corr, (symbols[i], symbols[j])
        context = {"pair": list(best_pair), "correlation": round(best, 4)} if best_pair else {}
        return best, context

    # ---------------------------------------------------------------- publish helpers

    async def _publish_risk(
        self,
        risk_type: str,
        *,
        value: float,
        threshold: float | None,
        message: str,
        symbol: str | None = None,
        applicable: bool = True,
        severity: Literal["info", "warning", "critical"] | None = None,
        context: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        if severity is None:
            ratio = (value / threshold) if (threshold is not None and threshold > 0) else 0.0
            severity = _severity_for(ratio)

        event = RiskEvent(
            source="CapitalProtectionEngine",
            timestamp=self._clock.now(),
            correlation_id=correlation_id,
            risk_type=risk_type,
            symbol=symbol,
            severity=severity,
            value=value,
            threshold=threshold,
            applicable=applicable,
            profile_name=self._profiles.current().name,
            message=message,
            context=context or {},
        )
        key = f"{risk_type}:{symbol}" if symbol else risk_type
        self._latest[key] = event
        log.info("risk_event_evaluated", risk_type=risk_type, symbol=symbol, severity=severity, value=round(value, 4))
        await self._publish_event(event)

    async def _publish_event(self, event: Any) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(event)

    # ---------------------------------------------------------------- queries / profile control

    def status(self) -> CapitalProtectionStatus:
        """The current, complete snapshot — what ``/risk`` renders."""
        open_notional = sum(p.notional for p in self._open_positions.values())
        return CapitalProtectionStatus(
            enabled=self._enabled,
            active_profile=self._profiles.current().name,
            equity=self._equity,
            peak_equity=self._peak_equity,
            daily_drawdown_pct=self._daily_drawdown_pct(),
            total_drawdown_pct=self._total_drawdown_pct(),
            trailing_drawdown_pct=self._trailing_drawdown_pct(),
            consecutive_losses=self._consecutive_losses,
            open_position_count=len(self._open_positions),
            open_position_notional=round(open_notional, 2),
            latest_risk_events=dict(self._latest),
            profile_names=self._profiles.names(),
        )

    def latest_for(self, risk_type: str, symbol: str | None = None) -> RiskEvent | None:
        key = f"{risk_type}:{symbol}" if symbol else risk_type
        return self._latest.get(key)

    def set_active_profile(self, name: str) -> bool:
        """Switches the active Risk Profile — the entire mechanism behind
        "profile switching without code modifications" (see
        ``app/capital_protection/profiles.py::RiskProfileRegistry.set_active``).
        The very next evaluation (the next ``DecisionRecorded`` or
        throttled ``MarketDataUpdated``) uses the newly active profile's
        thresholds."""
        return self._profiles.set_active(name)

    def profile_names(self) -> list[str]:
        return self._profiles.names()
