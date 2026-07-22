"""
CCI (Commodity Channel Index) — momentum oscillator.

Unlike RSI's mean-reversion framing, CCI readings beyond +/-100 are
conventionally read as trend *continuation* signals, not exhaustion —
that's the interpretation this plugin's evidence uses. Edge-triggered on
crossing +/-100, same shape as the RSI and ADX threshold crossings.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import cci as calculate_cci
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class CCIPlugin(PluginBase):
    """20-period CCI (configurable) with +/-100 breakout evidence."""

    name = "CCI"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 20))
        self._threshold: float = float(context.plugin_config.get("threshold", 100))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_cci: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("cci_plugin_initialized", period=self._period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("cci_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "threshold": self._threshold, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        value = calculate_cci(window.highs, window.lows, window.closes, self._period)
        if value is None:
            return

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name, symbol=event.symbol, indicator="CCI", value=round(value, 4), timeframe=event.timeframe
            )
        )

        prev = self._prev_cci[event.symbol]
        self._prev_cci[event.symbol] = value
        if prev is None:
            return

        crossed_up = prev <= self._threshold < value
        crossed_down = prev >= -self._threshold > value
        if not (crossed_up or crossed_down):
            return

        direction = "bullish" if crossed_up else "bearish"
        confidence = min(90.0, 55.0 + abs(value - (self._threshold if crossed_up else -self._threshold)) / 2)

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.MOMENTUM,
            title=f"CCI Breakout Above {int(self._threshold)}" if crossed_up else f"CCI Breakdown Below -{int(self._threshold)}",
            score=7,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"cci": round(value, 2), "period": self._period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("cci_threshold_crossed", symbol=event.symbol, direction=direction, cci=value)
