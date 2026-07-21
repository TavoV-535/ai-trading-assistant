"""
EMA (Exponential Moving Average) cross indicator.

Reference plugin — demonstrates the full Universal Plugin Contract end to
end: subscribes to ``MarketDataUpdated``, maintains rolling EMAs per symbol,
publishes ``IndicatorCalculated`` on every update, and publishes
``EvidenceProduced`` when a fast/slow cross happens. This plugin never
decides anything — it only ever hands the Reasoning Engine evidence.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.event_bus import EvidenceProduced, IndicatorCalculated, MarketDataUpdated
from app.evidence import Evidence, EvidenceCategory
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class _SymbolState:
    """Rolling EMA state for one symbol."""

    __slots__ = ("fast_ema", "slow_ema", "prev_fast", "prev_slow", "updates")

    def __init__(self) -> None:
        self.fast_ema: float | None = None
        self.slow_ema: float | None = None
        self.prev_fast: float | None = None
        self.prev_slow: float | None = None
        self.updates: int = 0


def _ema_step(previous: float | None, price: float, period: int) -> float:
    if previous is None:
        return price
    alpha = 2 / (period + 1)
    return price * alpha + previous * (1 - alpha)


class EMAPlugin(PluginBase):
    """Tracks fast/slow EMAs per symbol and flags bullish/bearish crosses."""

    name = "EMA"
    version = "0.1.0"
    category = "indicators"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._fast_period: int = int(context.plugin_config.get("fast", 20))
        self._slow_period: int = int(context.plugin_config.get("slow", 50))
        self._state: dict[str, _SymbolState] = defaultdict(_SymbolState)
        self._subscription = None
        self._last_event_at: datetime | None = None

    async def initialize(self) -> None:
        self._subscription = self.context.event_bus.subscribe(MarketDataUpdated, self._on_market_data)
        log.info("ema_plugin_initialized", fast=self._fast_period, slow=self._slow_period)

    async def shutdown(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
        log.info("ema_plugin_shutdown")

    async def health(self) -> PluginHealth:
        if self._last_event_at is None:
            return PluginHealth(status="degraded", detail="no market data received yet")
        return PluginHealth(status="healthy", detail=f"tracking {len(self._state)} symbol(s)")

    def config(self) -> dict[str, Any]:
        return {"fast": self._fast_period, "slow": self._slow_period}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_SUBSCRIBE, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- handler

    async def _on_market_data(self, event: MarketDataUpdated) -> None:
        self._last_event_at = datetime.now(timezone.utc)
        state = self._state[event.symbol]

        state.prev_fast, state.prev_slow = state.fast_ema, state.slow_ema
        state.fast_ema = _ema_step(state.fast_ema, event.price, self._fast_period)
        state.slow_ema = _ema_step(state.slow_ema, event.price, self._slow_period)
        state.updates += 1

        await self.context.event_bus.publish(
            IndicatorCalculated(
                source=self.name,
                symbol=event.symbol,
                indicator="EMA",
                value={"fast": round(state.fast_ema, 4), "slow": round(state.slow_ema, 4)},
                timeframe=event.timeframe,
            )
        )

        if state.updates < 2 or state.prev_fast is None or state.prev_slow is None:
            return  # not enough history yet to detect a cross

        crossed_bullish = state.prev_fast <= state.prev_slow and state.fast_ema > state.slow_ema
        crossed_bearish = state.prev_fast >= state.prev_slow and state.fast_ema < state.slow_ema

        if not (crossed_bullish or crossed_bearish):
            return

        direction = "bullish" if crossed_bullish else "bearish"
        spread_pct = abs(state.fast_ema - state.slow_ema) / state.slow_ema * 100 if state.slow_ema else 0.0
        confidence = min(95.0, 60.0 + spread_pct * 10)  # wider cross separation -> higher confidence, capped

        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.TREND,
            title=f"{direction.capitalize()} EMA Cross",
            score=15,
            confidence=confidence,
            direction=direction,
            symbol=event.symbol,
            timeframe=event.timeframe,
            metadata={"fast": self._fast_period, "slow": self._slow_period},
        )
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        log.info("ema_cross_detected", symbol=event.symbol, direction=direction, confidence=confidence)
