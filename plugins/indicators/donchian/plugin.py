"""
Donchian Channel — highest-high / lowest-low breakout channel.

Classic turtle-trading-style breakout: evidence fires when the current
close pushes past the *prior* update's channel boundary (not the channel
including the current bar, which would trivially always contain the
current bar's own high/low).
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


class DonchianPlugin(PluginBase):
    """20-period Donchian Channel (configurable) with breakout evidence."""

    name = "Donchian"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 20))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_channel: dict[str, tuple[float, float] | None] = defaultdict(lambda: None)  # (upper, lower)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("donchian_plugin_initialized", period=self._period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("donchian_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

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
        if not (broke_upper or broke_lower):
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
            metadata={"close": close, "prior_upper": round(prev_upper, 4), "prior_lower": round(prev_lower, 4), "period": self._period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("donchian_breakout_detected", symbol=event.symbol, direction=direction)
