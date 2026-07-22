"""
Supertrend — ATR-based trailing trend line.

Evidence fires when the trend flips (the line jumps from below price to
above it, or vice versa) — Supertrend's whole purpose is to be a binary
trend-direction flag, so unlike ATR/Bollinger this one is directional by
construction, not just a volatility measure.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import supertrend as calculate_supertrend
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class SupertrendPlugin(PluginBase):
    """10-period, 3x-multiplier Supertrend (configurable) with flip evidence."""

    name = "Supertrend"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 10))
        self._multiplier: float = float(context.plugin_config.get("multiplier", 3.0))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_direction: dict[str, str | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("supertrend_plugin_initialized", period=self._period, multiplier=self._multiplier)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("supertrend_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "multiplier": self._multiplier, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        result = calculate_supertrend(window.highs, window.lows, window.closes, self._period, self._multiplier)
        if result is None:
            return
        value, direction = result

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="Supertrend",
                value={"value": round(value, 4), "direction": direction},
                timeframe=event.timeframe,
            )
        )

        prev_direction = self._prev_direction[event.symbol]
        self._prev_direction[event.symbol] = direction
        if prev_direction is None or prev_direction == direction:
            return  # first reading, or trend hasn't flipped

        evidence_direction = "bullish" if direction == "up" else "bearish"
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title=f"Supertrend Flipped {evidence_direction.capitalize()}",
            score=13,
            confidence=75.0,
            direction=evidence_direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"value": round(value, 4), "period": self._period, "multiplier": self._multiplier},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("supertrend_flip_detected", symbol=event.symbol, direction=evidence_direction)
