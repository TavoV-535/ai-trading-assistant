"""
Ichimoku Cloud (Kumo) — trend structure from Tenkan/Kijun/Senkou spans.

Evidence fires when price crosses the entire cloud (both Senkou spans) —
a full cloud breakout is a stronger, less noisy signal than a single-line
cross, which is why this plugin (unlike SMA/EMA) doesn't fire on every
Tenkan/Kijun cross, only when close moves from inside-or-below the cloud to
above both spans, or vice versa.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import ichimoku as calculate_ichimoku
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class IchimokuPlugin(PluginBase):
    """Standard 9/26/52 Ichimoku Cloud (configurable) with cloud-breakout evidence."""

    name = "Ichimoku"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._tenkan_period: int = int(context.plugin_config.get("tenkan_period", 9))
        self._kijun_period: int = int(context.plugin_config.get("kijun_period", 26))
        self._senkou_b_period: int = int(context.plugin_config.get("senkou_b_period", 52))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_position: dict[str, str | None] = defaultdict(lambda: None)  # "above" | "inside" | "below"
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("ichimoku_plugin_initialized", tenkan=self._tenkan_period, kijun=self._kijun_period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("ichimoku_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {
            "tenkan_period": self._tenkan_period,
            "kijun_period": self._kijun_period,
            "senkou_b_period": self._senkou_b_period,
            "window": self._window_size,
        }

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        result = calculate_ichimoku(window.highs, window.lows, self._tenkan_period, self._kijun_period, self._senkou_b_period)
        if result is None:
            return

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="Ichimoku",
                value={k: round(v, 4) for k, v in result.items()},
                timeframe=event.timeframe,
            )
        )

        close = window.closes[-1]
        cloud_top = max(result["senkou_a"], result["senkou_b"])
        cloud_bottom = min(result["senkou_a"], result["senkou_b"])
        position = "above" if close > cloud_top else "below" if close < cloud_bottom else "inside"

        prev_position = self._prev_position[event.symbol]
        self._prev_position[event.symbol] = position

        if prev_position is None or position == prev_position or position == "inside":
            return  # no clean breakout of the whole cloud this update

        direction = "bullish" if position == "above" else "bearish"
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title=f"Price Broke {'Above' if direction == 'bullish' else 'Below'} Ichimoku Cloud",
            score=11,
            confidence=72.0,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"close": close, "senkou_a": round(result["senkou_a"], 4), "senkou_b": round(result["senkou_b"], 4)},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("ichimoku_cloud_breakout_detected", symbol=event.symbol, direction=direction)
