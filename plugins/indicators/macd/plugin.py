"""
MACD (Moving Average Convergence Divergence).

Publishes the MACD line, signal line, and histogram on every update, and
evidence when the MACD line crosses its own signal line — the standard
MACD trading signal, detected the same way EMAPlugin detects a fast/slow
EMA cross.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import macd as calculate_macd
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class MACDPlugin(PluginBase):
    """12/26/9 MACD (configurable) with signal-line-cross evidence."""

    name = "MACD"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._fast: int = int(context.plugin_config.get("fast", 12))
        self._slow: int = int(context.plugin_config.get("slow", 26))
        self._signal: int = int(context.plugin_config.get("signal", 9))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_histogram: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("macd_plugin_initialized", fast=self._fast, slow=self._slow, signal=self._signal)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("macd_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"fast": self._fast, "slow": self._slow, "signal": self._signal, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        result = calculate_macd(window.closes, self._fast, self._slow, self._signal)
        if result is None:
            return
        macd_line, signal_line, histogram = result

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="MACD",
                value={"macd": round(macd_line, 4), "signal": round(signal_line, 4), "histogram": round(histogram, 4)},
                timeframe=event.timeframe,
            )
        )

        prev_histogram = self._prev_histogram[event.symbol]
        self._prev_histogram[event.symbol] = histogram
        if prev_histogram is None:
            return

        crossed_bullish = prev_histogram <= 0 and histogram > 0
        crossed_bearish = prev_histogram >= 0 and histogram < 0
        if not (crossed_bullish or crossed_bearish):
            return

        direction = "bullish" if crossed_bullish else "bearish"
        confidence = min(95.0, 55.0 + abs(histogram) * 20)

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.MOMENTUM,
            title=f"{direction.capitalize()} MACD Signal Cross",
            score=12,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"fast": self._fast, "slow": self._slow, "signal": self._signal, "histogram": round(histogram, 4)},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("macd_cross_detected", symbol=event.symbol, direction=direction)
