"""
SMA (Simple Moving Average) cross indicator.

Same shape as the reference ``EMAPlugin`` — fast/slow moving average cross
detection — but using an unweighted simple average instead of an
exponential one. Demonstrates that a second moving-average-style indicator
adds zero duplicate math: both plugins ultimately just read
``SymbolWindow.closes`` and call into ``app.indicators.math``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import sma
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class SMAPlugin(PluginBase):
    """Tracks fast/slow SMAs per symbol and flags bullish/bearish crosses."""

    name = "SMA"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._fast_period: int = int(context.plugin_config.get("fast", 20))
        self._slow_period: int = int(context.plugin_config.get("slow", 50))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_fast: dict[str, float | None] = defaultdict(lambda: None)
        self._prev_slow: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("sma_plugin_initialized", fast=self._fast_period, slow=self._slow_period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("sma_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"fast": self._fast_period, "slow": self._slow_period, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        fast = sma(window.closes, self._fast_period)
        slow = sma(window.closes, self._slow_period)
        if fast is None or slow is None:
            return  # not enough history yet

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="SMA",
                value={"fast": round(fast, 4), "slow": round(slow, 4)},
                timeframe=event.timeframe,
            )
        )

        prev_fast, prev_slow = self._prev_fast[event.symbol], self._prev_slow[event.symbol]
        self._prev_fast[event.symbol], self._prev_slow[event.symbol] = fast, slow

        if prev_fast is None or prev_slow is None:
            return  # first reading, nothing to compare against yet

        crossed_bullish = prev_fast <= prev_slow and fast > slow
        crossed_bearish = prev_fast >= prev_slow and fast < slow
        if not (crossed_bullish or crossed_bearish):
            return

        direction = "bullish" if crossed_bullish else "bearish"
        spread_pct = abs(fast - slow) / slow * 100 if slow else 0.0
        confidence = min(95.0, 55.0 + spread_pct * 10)

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title=f"{direction.capitalize()} SMA Cross",
            score=10,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"fast": self._fast_period, "slow": self._slow_period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("sma_cross_detected", symbol=event.symbol, direction=direction, confidence=confidence)
