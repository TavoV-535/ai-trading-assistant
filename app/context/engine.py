"""
The Market Context Engine.

The rest of the platform reasons about individual pieces of evidence
("Bullish EMA Cross", "Positive Earnings Surprise", ...). This module's
job is different: reason about the *environment* those pieces of evidence
are appearing in — is the market trending or choppy, calm or violent,
about to hear from the Fed, thinly traded because it's a holiday. Those
higher-level labels change how the same piece of evidence should be read
(a bullish RSI cross means something different in a Bull Trend than in a
Low Liquidity holiday session), so they're modeled as their own thing:
:class:`~app.event_bus.events.MarketContextUpdated` events, published
independently of any specific symbol's evidence stream.

Inputs, per the architecture in PROJECT.md's Milestone 7 spec:

- **Market Data** — a bounded rolling window of closes/volumes per symbol,
  built directly from ``MarketDataUpdated`` (this engine keeps its own
  lightweight history; it does not call the Scanner Engine or any
  indicator plugin).
- **External Intelligence** — raw ``EvidenceProduced`` events carrying a
  ``metadata["context_hint"]`` convention (see the intelligence plugins
  under ``plugins/intelligence/``), promoted into calendar/macro context
  like "Fed Week" or "CPI Day".
- **Historical State** — this engine's own accumulated rolling windows and
  currently-active labels; nothing is persisted outside the process today
  (a future milestone could back it with the database layer without
  changing this module's public shape).

Every derivation below is a real, computed signal from data actually
flowing through the system — nothing is a hardcoded label. Context is
**edge-triggered**: a ``MarketContextUpdated`` event is only published
when a label actually changes for a given ``(symbol, context_type)`` key,
never on every tick, matching the same "don't spam the bus" convention
used everywhere else in this codebase (Strategy Engine's ``StrategyMatched``,
the Scanner Engine, ...).

This engine remains independent: it never calls the Evidence Aggregator,
Strategy Engine, or Reasoning Engine directly — only ``MarketContextUpdated``
leaves this module, and only through the Event Bus. Downstream systems
(the Confidence Weighting Framework, the Reasoning Engine) subscribe to
that event exactly like they subscribe to anything else.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from app.event_bus.bus import EventBus
from app.event_bus.events import EvidenceProduced, MarketContextUpdated, MarketDataUpdated
from app.logging import get_logger

log = get_logger(__name__)

#: Known macro/calendar context hints -> the human-readable label
#: published as MarketContextUpdated. An intelligence plugin publishing a
#: hint not listed here still gets promoted -- the label just falls back
#: to a title-cased version of the hint itself (see ``_on_evidence``) --
#: so adding a new macro context type never requires touching this file.
_CONTEXT_HINT_LABELS: dict[str, str] = {
    "fed_week": "Fed Week",
    "fed_meeting": "Fed Week",
    "cpi_release": "CPI Day",
    "cpi_day": "CPI Day",
    "jobs_report": "Jobs Report Day",
    "earnings_season": "Earnings Season",
    "holiday_session": "Holiday Session",
    "government_event": "Government Event",
    "treasury_auction": "Treasury Auction Day",
}


@dataclass
class _SymbolHistory:
    closes: "deque[float]" = field(default_factory=lambda: deque(maxlen=64))
    volumes: "deque[float]" = field(default_factory=lambda: deque(maxlen=64))
    last_price: float | None = None


class MarketContextEngine:
    """Continuously derives market-environment context and publishes
    :class:`~app.event_bus.events.MarketContextUpdated`. Attach once at
    bootstrap, before or after the Evidence Aggregator — subscription
    order doesn't matter, only that ``attach()`` runs before events start
    flowing."""

    def __init__(self, settings: Any) -> None:
        section = getattr(settings, "context", None)
        self._trend_window = int(getattr(section, "trend_window", 20))
        self._trend_bull_threshold = float(getattr(section, "trend_bull_threshold_pct", 1.5))
        self._trend_bear_threshold = float(getattr(section, "trend_bear_threshold_pct", -1.5))
        self._volatility_window = int(getattr(section, "volatility_window", 20))
        self._high_volatility_threshold = float(getattr(section, "high_volatility_threshold_pct", 2.0))
        self._low_volatility_threshold = float(getattr(section, "low_volatility_threshold_pct", 0.3))
        self._gap_threshold_pct = float(getattr(section, "gap_threshold_pct", 2.0))
        self._low_liquidity_ratio = float(getattr(section, "low_liquidity_volume_ratio", 0.4))
        self._risk_regime_min_symbols = int(getattr(section, "risk_regime_min_symbols", 2))
        self._risk_regime_majority = float(getattr(section, "risk_regime_majority_ratio", 0.6))

        self._history: dict[str, _SymbolHistory] = defaultdict(_SymbolHistory)
        #: (symbol, context_type) -> currently-active label. ``symbol`` is
        #: None for market-wide context. This is the "Historical State"
        #: input the class docstring refers to -- it's what makes context
        #: publishing edge-triggered instead of re-announcing the same
        #: label every tick.
        self._current: dict[tuple[str | None, str], str] = {}
        self._event_bus: EventBus | None = None

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(MarketDataUpdated, self._on_market_data, name="market_context_engine")
        event_bus.subscribe(EvidenceProduced, self._on_evidence, name="market_context_engine_intelligence")
        log.info("market_context_engine_attached", trend_window=self._trend_window)

    def snapshot(self, symbol: str | None = None) -> dict[str, str]:
        """Current context labels for ``symbol`` (or market-wide context
        when ``symbol`` is ``None``), keyed by ``context_type`` — usable
        on-demand without waiting for the next event, mirroring
        ``EvidenceAggregator.snapshot()``."""
        return {ctype: label for (sym, ctype), label in self._current.items() if sym == symbol}

    # ---------------------------------------------------------------- market data

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        hist = self._history[event.symbol]
        price = event.close if event.close is not None else event.price
        prev_price = hist.last_price

        # Gap detection runs on the *transition* into this update, before
        # it's folded into the rolling window below.
        if prev_price and prev_price > 0:
            change_pct = (price - prev_price) / prev_price * 100
            if abs(change_pct) >= self._gap_threshold_pct:
                await self._publish(event.symbol, "gap", "Gap Day", metadata={"change_percent": round(change_pct, 2)})

        hist.closes.append(price)
        if event.volume is not None:
            hist.volumes.append(float(event.volume))
        hist.last_price = price

        await self._update_trend_and_exhaustion(event.symbol, hist)
        await self._update_volatility(event.symbol, hist)
        await self._update_liquidity(event.symbol, hist)
        await self._update_risk_regime()

    async def _update_trend_and_exhaustion(self, symbol: str, hist: _SymbolHistory) -> None:
        closes = list(hist.closes)
        if len(closes) < max(5, self._trend_window // 2):
            return

        window = closes[-self._trend_window :]
        change_pct = (window[-1] - window[0]) / window[0] * 100 if window[0] else 0.0
        if change_pct >= self._trend_bull_threshold:
            label = "Bull Trend"
        elif change_pct <= self._trend_bear_threshold:
            label = "Bear Trend"
        else:
            label = "Sideways Market"
        await self._publish(symbol, "trend", label, metadata={"change_percent": round(change_pct, 2)})

        # Trend Exhaustion: currently trending, but the most recent half
        # of the window has decelerated to under 30% of the first half's
        # move (or reversed outright) -- a real, if simple, momentum-
        # deceleration check computed from the same window, not a
        # hardcoded guess.
        if len(window) >= 6 and label in ("Bull Trend", "Bear Trend") and window[0] and window[len(window) // 2]:
            mid = len(window) // 2
            first_leg_pct = (window[mid] - window[0]) / window[0] * 100
            second_leg_pct = (window[-1] - window[mid]) / window[mid] * 100
            decelerating = (
                label == "Bull Trend" and first_leg_pct > 0 and second_leg_pct < first_leg_pct * 0.3
            ) or (label == "Bear Trend" and first_leg_pct < 0 and second_leg_pct > first_leg_pct * 0.3)
            if decelerating:
                await self._publish(
                    symbol,
                    "exhaustion",
                    "Trend Exhaustion",
                    metadata={"first_leg_pct": round(first_leg_pct, 2), "second_leg_pct": round(second_leg_pct, 2)},
                )
                return
        self._clear(symbol, "exhaustion")

    async def _update_volatility(self, symbol: str, hist: _SymbolHistory) -> None:
        closes = list(hist.closes)
        if len(closes) < 5:
            return
        window = closes[-self._volatility_window :]
        returns = [(window[i] - window[i - 1]) / window[i - 1] * 100 for i in range(1, len(window)) if window[i - 1]]
        if not returns:
            return
        mean = sum(returns) / len(returns)
        stdev = (sum((r - mean) ** 2 for r in returns) / len(returns)) ** 0.5

        if stdev >= self._high_volatility_threshold:
            await self._publish(symbol, "volatility", "High Volatility", metadata={"stdev_pct": round(stdev, 3)})
        elif stdev <= self._low_volatility_threshold:
            await self._publish(symbol, "volatility", "Low Volatility", metadata={"stdev_pct": round(stdev, 3)})
        else:
            self._clear(symbol, "volatility")

    async def _update_liquidity(self, symbol: str, hist: _SymbolHistory) -> None:
        volumes = list(hist.volumes)
        if len(volumes) < 5:
            return
        prior = volumes[:-1]
        avg = sum(prior) / len(prior)
        current = volumes[-1]
        if avg > 0 and current / avg <= self._low_liquidity_ratio:
            await self._publish(symbol, "liquidity", "Low Liquidity", metadata={"volume_ratio": round(current / avg, 3)})
        else:
            self._clear(symbol, "liquidity")

    async def _update_risk_regime(self) -> None:
        """Market-wide (``symbol=None``) Risk-On/Risk-Off, derived from
        what fraction of currently-tracked symbols are in a Bull vs. Bear
        trend -- a genuine cross-symbol aggregate, not a per-symbol
        computation relabeled."""
        trend_labels = [label for (sym, ctype), label in self._current.items() if ctype == "trend" and sym is not None]
        if len(trend_labels) < self._risk_regime_min_symbols:
            return
        total = len(trend_labels)
        bull_ratio = sum(1 for label in trend_labels if label == "Bull Trend") / total
        bear_ratio = sum(1 for label in trend_labels if label == "Bear Trend") / total

        if bull_ratio >= self._risk_regime_majority:
            await self._publish(None, "risk_regime", "Risk-On", metadata={"bull_ratio": round(bull_ratio, 2)})
        elif bear_ratio >= self._risk_regime_majority:
            await self._publish(None, "risk_regime", "Risk-Off", metadata={"bear_ratio": round(bear_ratio, 2)})
        else:
            self._clear(None, "risk_regime")

    # ---------------------------------------------------------------- intelligence

    async def _on_evidence(self, event: EvidenceProduced) -> None:
        """Promotes calendar/macro-style intelligence evidence into
        context. Keyed off a ``metadata["context_hint"]`` convention any
        External Intelligence Platform plugin can use — this engine never
        needs to know which specific intelligence plugin published it."""
        hint = event.evidence.metadata.get("context_hint")
        if not hint:
            return
        label = _CONTEXT_HINT_LABELS.get(hint, hint.replace("_", " ").title())
        await self._publish(
            event.evidence.symbol,
            "macro_event",
            label,
            metadata={"source": event.evidence.source, "hint": hint},
        )

    # ---------------------------------------------------------------- publish

    async def _publish(
        self, symbol: str | None, context_type: str, label: str, *, metadata: dict[str, Any] | None = None
    ) -> None:
        key = (symbol, context_type)
        if self._current.get(key) == label:
            return  # edge-triggered -- no change, no event
        self._current[key] = label
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            MarketContextUpdated(
                source="MarketContextEngine",
                symbol=symbol,
                context_type=context_type,
                label=label,
                metadata=metadata or {},
            )
        )
        log.debug("market_context_updated", symbol=symbol, context_type=context_type, label=label)

    def _clear(self, symbol: str | None, context_type: str) -> None:
        """Drops a context bucket that no longer holds (e.g. volatility
        returning to normal) without publishing an event for it -- the
        absence of an active label for that ``(symbol, context_type)`` key
        *is* the "back to normal" signal. Only buckets with a genuine
        replacement label (like trend, which always has one of Bull/Bear/
        Sideways) go through ``_publish`` on every change."""
        self._current.pop((symbol, context_type), None)
