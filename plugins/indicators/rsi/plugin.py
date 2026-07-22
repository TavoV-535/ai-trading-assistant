"""
RSI (Relative Strength Index) — momentum oscillator.

Publishes evidence when RSI crosses into overbought (>70, default) or
oversold (<30, default) territory. Edge-triggered on the threshold crossing
(not "RSI is currently above 70") so a symbol sitting in overbought
territory for an hour doesn't spam a fresh piece of evidence on every tick —
the same cross-detection shape as ``EMAPlugin``'s fast/slow cross, just
against a fixed threshold instead of another moving average.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import rsi as calculate_rsi
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class RSIPlugin(PluginBase):
    """14-period RSI (configurable) with overbought/oversold evidence."""

    name = "RSI"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 14))
        self._overbought: float = float(context.plugin_config.get("overbought", 70))
        self._oversold: float = float(context.plugin_config.get("oversold", 30))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_rsi: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("rsi_plugin_initialized", period=self._period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("rsi_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {
            "period": self._period,
            "overbought": self._overbought,
            "oversold": self._oversold,
            "window": self._window_size,
        }

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        value = calculate_rsi(window.closes, self._period)
        if value is None:
            return

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name, symbol=event.symbol, indicator="RSI", value=round(value, 4), timeframe=event.timeframe
            )
        )

        prev = self._prev_rsi[event.symbol]
        self._prev_rsi[event.symbol] = value
        if prev is None:
            return

        crossed_overbought = prev <= self._overbought < value
        crossed_oversold = prev >= self._oversold > value
        if not (crossed_overbought or crossed_oversold):
            return

        if crossed_overbought:
            direction, title = "bearish", "RSI Overbought"
            confidence = min(95.0, 55.0 + (value - self._overbought) * 2)
        else:
            direction, title = "bullish", "RSI Oversold"
            confidence = min(95.0, 55.0 + (self._oversold - value) * 2)

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.MOMENTUM,
            title=title,
            score=8,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"rsi": round(value, 2), "period": self._period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("rsi_threshold_crossed", symbol=event.symbol, direction=direction, rsi=value)
