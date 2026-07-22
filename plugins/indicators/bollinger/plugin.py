"""
Bollinger Bands — volatility bands around a moving average.

Evidence fires (edge-triggered) when a close breaks outside the bands —
the standard breakout interpretation (momentum continuing through
increasing volatility), not the mean-reversion interpretation. Both
schools of thought exist in technical analysis; this plugin picks one and
says so explicitly in its evidence metadata/title so the Reasoning Engine
(and the person reading its output) knows exactly what claim is being made.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import bollinger_bands
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class BollingerPlugin(PluginBase):
    """20-period Bollinger Bands (configurable) with breakout evidence."""

    name = "Bollinger"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 20))
        self._num_std: float = float(context.plugin_config.get("num_std", 2.0))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_outside: dict[str, str | None] = defaultdict(lambda: None)  # "upper" | "lower" | None
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("bollinger_plugin_initialized", period=self._period, num_std=self._num_std)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("bollinger_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "num_std": self._num_std, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        result = bollinger_bands(window.closes, self._period, self._num_std)
        if result is None:
            return
        upper, mid, lower = result
        close = window.closes[-1]

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="Bollinger",
                value={"upper": round(upper, 4), "mid": round(mid, 4), "lower": round(lower, 4)},
                timeframe=event.timeframe,
            )
        )

        currently_outside = "upper" if close > upper else "lower" if close < lower else None
        prev_outside = self._prev_outside[event.symbol]
        self._prev_outside[event.symbol] = currently_outside

        if currently_outside is None or currently_outside == prev_outside:
            return  # no new breakout since the last update

        direction = "bullish" if currently_outside == "upper" else "bearish"
        band_width_pct = (upper - lower) / mid * 100 if mid else 0.0
        confidence = min(90.0, 55.0 + band_width_pct)

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.VOLATILITY,
            title=f"Bollinger Band Breakout ({currently_outside.capitalize()})",
            score=9,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"close": close, "upper": round(upper, 4), "lower": round(lower, 4), "period": self._period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("bollinger_breakout_detected", symbol=event.symbol, direction=direction, band=currently_outside)
