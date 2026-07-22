"""
Volume Profile — volume distribution across price levels within the
retained window, and the resulting Point of Control (POC).

Evidence fires when price crosses the POC — the POC acts as a
volume-based magnet/support-resistance level, so a clean cross of it is
treated as a directional signal, the same edge-triggered shape used
throughout this indicator library.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import volume_profile as calculate_volume_profile
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class VolumeProfilePlugin(PluginBase):
    """Volume-weighted price distribution with POC-cross evidence."""

    name = "VolumeProfile"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._num_bins: int = int(context.plugin_config.get("num_bins", 10))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_side: dict[str, str | None] = defaultdict(lambda: None)  # "above" | "below" of POC
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("volume_profile_plugin_initialized", num_bins=self._num_bins)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("volume_profile_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        if not any(w.has_volume for w in self._windows.values()):
            return PluginHealth(status="degraded", detail="no volume data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"num_bins": self._num_bins, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        result = calculate_volume_profile(window.closes, window.volumes, self._num_bins)
        if result is None:
            return
        poc = result["poc"]
        close = window.closes[-1]

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="VolumeProfile",
                value={"poc": round(poc, 4)},
                timeframe=event.timeframe,
            )
        )

        side = "above" if close > poc else "below" if close < poc else None
        prev_side = self._prev_side[event.symbol]
        self._prev_side[event.symbol] = side

        if side is None or prev_side is None or side == prev_side:
            return

        direction = "bullish" if side == "above" else "bearish"
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.VOLUME,
            title=f"Price Crossed {'Above' if direction == 'bullish' else 'Below'} Volume POC",
            score=8,
            confidence=68.0,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"close": close, "poc": round(poc, 4), "num_bins": self._num_bins},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("volume_profile_poc_cross_detected", symbol=event.symbol, direction=direction)
