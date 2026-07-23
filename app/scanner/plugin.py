"""
The Scanner Plugin contract — the first continuous market-observation
system in the platform.

A scanner plugin repeatedly asks the Market Data Abstraction Layer (never
a specific provider — see ``app/marketdata/``) for the latest bar per
symbol/timeframe it's configured to watch, and publishes
``MarketDataUpdated`` for each one it gets. It never calls an indicator
plugin directly: indicator plugins already discover new data by
subscribing to ``MarketDataUpdated``, exactly like every other consumer of
that event, so a scanner ticking is indistinguishable from any other
source of the same event as far as the rest of the pipeline is concerned.

Concrete scanner plugins are expected to be almost entirely configuration
— watchlist, timeframes, interval_seconds — which is what makes "support
multiple watchlists" and "run multiple scanners simultaneously" true
without writing new Python: add another ``plugins/scanners/<name>/``
folder with its own ``config.yaml``. See ``plugins/scanners/core/`` for
the reference.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.event_bus.events import MarketDataUpdated
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class ScannerPlugin(PluginBase):
    """Base class for every continuous scanner plugin.

    Implements the Universal Plugin Contract generically — a concrete
    scanner (like ``CoreWatchlistScanner``) typically doesn't need to
    override anything at all, just supply its own ``config.yaml``.
    """

    category: str = "scanners"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        cfg = context.plugin_config
        self.watchlist: tuple[str, ...] = tuple(cfg.get("watchlist") or [])
        self.timeframes: tuple[str, ...] = tuple(cfg.get("timeframes") or context.settings.scanner.timeframes or ["1m"])
        self.interval_seconds: float = float(cfg.get("interval_seconds", context.settings.scanner.interval_seconds))
        self.asset_class: str = str(cfg.get("asset_class", "stocks"))
        self._task: "asyncio.Task[None] | None" = None
        self._ticks = 0
        self._last_error: str | None = None

    # ---------------------------------------------------------------- contract

    async def initialize(self) -> None:
        if not self.watchlist:
            self._last_error = "empty watchlist"
            log.warning("scanner_started_with_empty_watchlist", plugin=self.name)
        self._task = asyncio.create_task(self._run_loop(), name=f"scanner:{self.name}")
        log.info(
            "scanner_started",
            plugin=self.name,
            watchlist=list(self.watchlist),
            timeframes=list(self.timeframes),
            interval_seconds=self.interval_seconds,
        )

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        log.info("scanner_stopped", plugin=self.name, ticks=self._ticks)

    async def health(self) -> PluginHealth:
        if self._last_error:
            return PluginHealth(status="degraded", detail=f"{self._ticks} tick(s); {self._last_error}")
        return PluginHealth(status="healthy", detail=f"{self._ticks} tick(s)")

    def config(self) -> dict[str, Any]:
        return {
            "watchlist": list(self.watchlist),
            "timeframes": list(self.timeframes),
            "interval_seconds": self.interval_seconds,
            "asset_class": self.asset_class,
        }

    def permissions(self) -> list[str]:
        return [PluginPermission.MARKET_DATA_READ, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- scanning

    async def _run_loop(self) -> None:
        """Ticks forever at ``interval_seconds`` until ``shutdown()``
        cancels this task. A failed tick is logged and retried on the next
        interval — the same "isolate, don't crash" discipline every other
        part of this platform follows; a scanner never takes the process
        down."""
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                log.exception("scanner_tick_failed", plugin=self.name)
            await asyncio.sleep(self.interval_seconds)

    async def scan_once(self) -> None:
        """One scan pass across every configured timeframe.

        Publishes ``MarketDataUpdated`` only — never calls an indicator
        plugin, the Evidence Aggregator, or the Strategy Engine directly.
        Everything downstream discovers new data by subscribing to the
        Event Bus, same as any other consumer.
        """
        market_data = self.context.market_data_service
        if market_data is None:
            self._last_error = "market_data_service not configured"
            log.warning("scanner_has_no_market_data_service", plugin=self.name)
            return
        if not self.watchlist:
            return

        symbols = list(self.watchlist)
        for timeframe in self.timeframes:
            bars = await market_data.fetch(symbols, timeframe)
            for symbol, bar in bars.items():
                await self.context.event_bus.publish(
                    MarketDataUpdated(
                        source=self.name,
                        symbol=symbol,
                        price=bar.close,
                        volume=int(bar.volume),
                        timeframe=timeframe,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                    )
                )

        self._ticks += 1
        self._last_error = None
