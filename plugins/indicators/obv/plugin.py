"""
OBV (On-Balance Volume) — cumulative volume signed by price direction.

Evidence fires when OBV crosses its own moving average — a standard OBV
confirmation technique (volume flow turning bullish/bearish), the same
cross-detection shape as EMAPlugin, just applied to OBV's own history
instead of price. Degrades to a "degraded" health status (rather than
silently publishing an all-zero indicator forever) if the market data feed
never carries real volume.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import obv as calculate_obv
from app.indicators.math import sma
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class OBVPlugin(PluginBase):
    """On-Balance Volume with moving-average-cross evidence."""

    name = "OBV"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._signal_period: int = int(context.plugin_config.get("signal_period", 20))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._obv_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self._window_size))
        self._prev_obv: dict[str, float | None] = defaultdict(lambda: None)
        self._prev_signal: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("obv_plugin_initialized", signal_period=self._signal_period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("obv_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        if not any(w.has_volume for w in self._windows.values()):
            return PluginHealth(status="degraded", detail="no volume data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"signal_period": self._signal_period, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        value = calculate_obv(window.closes, window.volumes)
        if value is None:
            return

        history = self._obv_history[event.symbol]
        history.append(value)

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name, symbol=event.symbol, indicator="OBV", value=round(value, 4), timeframe=event.timeframe
            )
        )

        signal = sma(list(history), self._signal_period)
        if signal is None:
            return

        prev_obv, prev_signal = self._prev_obv[event.symbol], self._prev_signal[event.symbol]
        self._prev_obv[event.symbol], self._prev_signal[event.symbol] = value, signal
        if prev_obv is None or prev_signal is None:
            return

        crossed_bullish = prev_obv <= prev_signal and value > signal
        crossed_bearish = prev_obv >= prev_signal and value < signal
        if not (crossed_bullish or crossed_bearish):
            return

        direction = "bullish" if crossed_bullish else "bearish"
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.VOLUME,
            title=f"OBV Crossed {'Above' if crossed_bullish else 'Below'} Signal",
            score=7,
            confidence=65.0,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"obv": round(value, 2), "signal": round(signal, 2), "signal_period": self._signal_period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("obv_signal_cross_detected", symbol=event.symbol, direction=direction)
