"""
ATR (Average True Range) — volatility measure.

ATR itself has no direction, so unlike the trend/momentum indicators this
plugin's evidence is deliberately ``direction="neutral"``: it flags a
volatility regime change (a sharp expansion relative to the recent reading)
without claiming to know which way price will move — that synthesis is the
Reasoning Engine's job, using this evidence alongside directional evidence
from other plugins (e.g. a wide ATR expansion alongside an EMA cross is a
very different situation than the same cross with flat ATR).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.indicators.bar import SymbolWindow, bar_from_event
from app.indicators.math import atr as calculate_atr
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class ATRPlugin(PluginBase):
    """14-period ATR (configurable) with volatility-expansion evidence."""

    name = "ATR"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._period: int = int(context.plugin_config.get("period", 14))
        self._expansion_ratio: float = float(context.plugin_config.get("expansion_ratio", 1.5))
        self._window_size: int = int(context.plugin_config.get("window", 300))
        self._windows: dict[str, SymbolWindow] = defaultdict(lambda: SymbolWindow(self._window_size))
        self._prev_atr: dict[str, float | None] = defaultdict(lambda: None)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("atr_plugin_initialized", period=self._period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("atr_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._windows)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"period": self._period, "expansion_ratio": self._expansion_ratio, "window": self._window_size}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        window = self._windows[event.symbol]
        window.append(bar_from_event(event))

        value = calculate_atr(window.highs, window.lows, window.closes, self._period)
        if value is None:
            return

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name, symbol=event.symbol, indicator="ATR", value=round(value, 4), timeframe=event.timeframe
            )
        )

        prev = self._prev_atr[event.symbol]
        self._prev_atr[event.symbol] = value
        if prev is None or prev <= 0:
            return

        if value < prev * self._expansion_ratio:
            return  # no meaningful expansion

        confidence = min(90.0, 50.0 + (value / prev - self._expansion_ratio) * 40)
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.VOLATILITY,
            title="Volatility Expansion (ATR)",
            score=6,
            confidence=confidence,
            direction="neutral",
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"atr": round(value, 4), "previous_atr": round(prev, 4), "period": self._period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("atr_expansion_detected", symbol=event.symbol, atr=value, previous_atr=prev)
