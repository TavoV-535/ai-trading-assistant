"""
ADX (Average Directional Index) with +DI/-DI.

ADX measures trend *strength*, not direction — direction comes from
comparing +DI and -DI. Evidence fires (edge-triggered) when ADX crosses
above the "trending" threshold (25 by default, the widely used convention),
with direction taken from whichever DI line is currently dominant.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import adx as calculate_adx
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class ADXPlugin(PluginBase):
    """14-period ADX (configurable) with trend-strength evidence."""

    name = "ADX"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 14))
        self._trend_threshold: float = float(context.plugin_config.get("trend_threshold", 25))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_adx: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("adx_plugin_initialized", period=self._period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("adx_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "trend_threshold": self._trend_threshold, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        result = calculate_adx(window.highs, window.lows, window.closes, self._period)
        if result is None:
            return
        adx_value, plus_di, minus_di = result

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="ADX",
                value={"adx": round(adx_value, 4), "plus_di": round(plus_di, 4), "minus_di": round(minus_di, 4)},
                timeframe=event.timeframe,
            )
        )

        prev = self._prev_adx[event.symbol]
        self._prev_adx[event.symbol] = adx_value
        if prev is None:
            return

        if not (prev <= self._trend_threshold < adx_value):
            return  # only fire on the crossing, not every bar spent above threshold

        direction = "bullish" if plus_di >= minus_di else "bearish"
        confidence = min(90.0, 50.0 + (adx_value - self._trend_threshold) * 1.5)

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title="Strong Trend Emerging (ADX)",
            score=10,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"adx": round(adx_value, 2), "plus_di": round(plus_di, 2), "minus_di": round(minus_di, 2)},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("adx_trend_threshold_crossed", symbol=event.symbol, direction=direction, adx=adx_value)
