"""
Donchian Channel — highest-high / lowest-low breakout channel.

Classic turtle-trading-style breakout: evidence fires when the current
close pushes past the *prior* update's channel boundary (not the channel
including the current bar, which would trivially always contain the
current bar's own high/low).

In a sustained trend, every single bar can be a fresh "new high" — that's
mathematically correct, not a bug, and this plugin never suppresses or
rate-limits that math. What's configurable instead is *how often it
publishes evidence* about it (``repeat_policy``), while every occurrence —
published or not — is still tracked, and every published piece of evidence
carries sequence metadata (``breakout_sequence``, ``bars_since_first_breakout``,
``is_first_in_sequence``, ``is_first_ever``, ``distance_from_channel``) so
that a Strategy Engine consuming this evidence can reinterpret repeats
differently per-strategy even when this plugin's own policy is
``every_breakout`` — see ``app.strategy.compiler``'s repeat-policy filter
and ``docs/PLUGIN_GUIDE.md``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import donchian_channel
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)

_VALID_REPEAT_POLICIES = {"every_breakout", "first_breakout", "after_pullback"}


class _SequenceState:
    """Tracks one symbol's current breakout streak, so repeated breakouts
    in the same direction can be told apart from the first one in a new
    sequence (i.e. following a pullback)."""

    __slots__ = ("streak_side", "sequence_number", "streak_start_bar", "had_prior_breakout")

    def __init__(self) -> None:
        self.streak_side: str | None = None  # "upper" | "lower" | None
        self.sequence_number: int = 0
        self.streak_start_bar: int = 0
        self.had_prior_breakout: bool = False


class DonchianPlugin(PluginBase):
    """20-period Donchian Channel (configurable) with breakout evidence."""

    name = "Donchian"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 20))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        repeat_policy = str(context.plugin_config.get("repeat_policy", "every_breakout")).strip().lower()
        if repeat_policy not in _VALID_REPEAT_POLICIES:
            log.warning("donchian_invalid_repeat_policy", configured=repeat_policy, falling_back_to="every_breakout")
            repeat_policy = "every_breakout"
        self._repeat_policy = repeat_policy
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_channel: dict[str, tuple[float, float] | None] = defaultdict(lambda: None)  # (upper, lower)
        self._sequences: dict[str, _SequenceState] = defaultdict(_SequenceState)
        self._bar_count: dict[str, int] = defaultdict(int)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("donchian_plugin_initialized", period=self._period, repeat_policy=self._repeat_policy)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("donchian_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "window": self._window_size, "repeat_policy": self._repeat_policy}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))
        self._bar_count[event.symbol] += 1
        bar_index = self._bar_count[event.symbol]

        result = donchian_channel(window.highs, window.lows, self._period)
        if result is None:
            return
        upper, lower, mid = result
        close = window.closes[-1]

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="Donchian",
                value={"upper": round(upper, 4), "lower": round(lower, 4), "mid": round(mid, 4)},
                timeframe=event.timeframe,
            )
        )

        prev_channel = self._prev_channel[event.symbol]
        self._prev_channel[event.symbol] = (upper, lower)
        if prev_channel is None:
            return

        prev_upper, prev_lower = prev_channel
        broke_upper = close > prev_upper
        broke_lower = close < prev_lower

        seq = self._sequences[event.symbol]
        if not (broke_upper or broke_lower):
            seq.streak_side = None  # a bar with no breakout ends any streak -> a real pullback
            return

        side = "upper" if broke_upper else "lower"
        is_first_in_sequence = seq.streak_side != side
        is_first_ever = is_first_in_sequence and not seq.had_prior_breakout

        if is_first_in_sequence:
            seq.sequence_number = 1
            seq.streak_start_bar = bar_index
        else:
            seq.sequence_number += 1
        seq.streak_side = side
        seq.had_prior_breakout = True

        bars_since_first_breakout = bar_index - seq.streak_start_bar
        distance_from_channel = (close - prev_upper) if broke_upper else (prev_lower - close)

        if not self._should_publish(is_first_in_sequence, is_first_ever):
            return

        direction = "bullish" if broke_upper else "bearish"
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title=f"Donchian Channel Breakout ({'New High' if broke_upper else 'New Low'})",
            score=10,
            confidence=70.0,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={
                "close": close,
                "prior_upper": round(prev_upper, 4),
                "prior_lower": round(prev_lower, 4),
                "period": self._period,
                "breakout_sequence": seq.sequence_number,
                "bars_since_first_breakout": bars_since_first_breakout,
                "is_first_in_sequence": is_first_in_sequence,
                "is_first_ever": is_first_ever,
                "distance_from_channel": round(distance_from_channel, 4),
            },
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info(
            "donchian_breakout_detected",
            symbol=event.symbol,
            direction=direction,
            sequence=seq.sequence_number,
            is_first_in_sequence=is_first_in_sequence,
        )

    def _should_publish(self, is_first_in_sequence: bool, is_first_ever: bool) -> bool:
        if self._repeat_policy == "first_breakout":
            return is_first_in_sequence
        if self._repeat_policy == "after_pullback":
            return is_first_in_sequence and not is_first_ever
        return True  # "every_breakout" (default) — publish every mathematically-correct breakout
