"""
VWAP (Volume-Weighted Average Price).

Evidence fires when price crosses VWAP — a widely used institutional
fair-value benchmark; the cross itself (not just "price is above VWAP",
which would fire on every single update) is the meaningful event, same
edge-triggered pattern as everything else in this library.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import vwap as calculate_vwap
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class VWAPPlugin(PluginBase):
    """Volume-weighted average price over the retained window, with cross evidence."""

    name = "VWAP"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_side: dict[str, str | None] = defaultdict(lambda: None)  # "above" | "below" of VWAP
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("vwap_plugin_initialized")

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("vwap_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        if not any(w.has_volume for w in self._windows.values()):
            return PluginHealth(status="degraded", detail="no volume data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        value = calculate_vwap(window.closes, window.highs, window.lows, window.volumes)
        if value is None:
            return
        close = window.closes[-1]

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name, symbol=event.symbol, indicator="VWAP", value=round(value, 4), timeframe=event.timeframe
            )
        )

        side = "above" if close > value else "below" if close < value else None
        prev_side = self._prev_side[event.symbol]
        self._prev_side[event.symbol] = side

        if side is None or prev_side is None or side == prev_side:
            return

        direction = "bullish" if side == "above" else "bearish"
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title=f"Price Crossed {'Above' if direction == 'bullish' else 'Below'} VWAP",
            score=9,
            confidence=66.0,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"close": close, "vwap": round(value, 4)},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("vwap_cross_detected", symbol=event.symbol, direction=direction)
