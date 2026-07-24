"""
The External Intelligence Platform's plugin contract.

PROJECT.md's Milestone 7 spec is explicit: don't build separate isolated
News, Earnings, Macro, SEC Filings, Insider Activity, or Economic
Calendar *engines*. Every non-price source of market information is
simply another plugin, producing the exact same two things every other
evidence producer in this codebase produces:

1. A normalized **Intelligence Event** — a typed, queryable fact
   (``NewsReceived``, ``EarningsReleased``, ``MacroEventOccurred``, ...;
   see ``app/event_bus/events.py``). Future sources (SEC filings, insider
   transactions, FDA approvals, M&A, buybacks, dividends, stock splits,
   treasury auctions, Fed speeches, ...) either reuse one of these or add
   a new small event schema — never a new subsystem.
2. A normalized **Evidence Object**, published exactly like an indicator
   plugin's — the Evidence Aggregator doesn't know or care whether a
   piece of evidence came from an RSI cross or a positive earnings
   surprise; it's evidence either way.

``IntelligencePlugin`` is the one shared piece of infrastructure every
intelligence plugin gets for free: a config-driven polling loop (mirroring
``ScannerPlugin``'s tick loop — most real intelligence sources are polled
on an interval in practice, whether that's a news API, an earnings
calendar, or an economic-release feed) and a ``_publish()`` helper that
keeps the (event, evidence) pair from ever drifting out of sync. A
concrete plugin implements one method, ``poll_once()``, and calls
``self._publish(...)`` for each new item it finds.

Like every other plugin in this codebase, an intelligence plugin never
generates a buy/sell recommendation and never bypasses the Event Bus —
``self.context.event_bus`` is the only way anything it does becomes
visible to the rest of the system.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.event_bus.events import Event, EvidenceProduced
from app.evidence.schema import Evidence
from app.logging import get_logger
from app.plugins.base import PluginBase, PluginHealth, PluginPermission

log = get_logger(__name__)


class IntelligencePlugin(PluginBase):
    """Base class for every External Intelligence Platform plugin.

    Concrete plugins (News, Earnings, Macro, and any future source)
    override ``poll_once()`` and call ``self._publish(intelligence_event,
    evidence)`` for each new item discovered on that pass. Everything
    else — the interval loop, failure isolation, health reporting — is
    handled generically here, the same "concrete plugin is almost entirely
    configuration + one method" shape as ``ScannerPlugin``.
    """

    category: str = "intelligence"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        cfg = context.plugin_config
        default_interval = getattr(getattr(context.settings, "intelligence", None), "interval_seconds", 60)
        self.interval_seconds: float = float(cfg.get("interval_seconds", default_interval))
        self._task: "asyncio.Task[None] | None" = None
        self._ticks = 0
        self._published_count = 0
        self._last_error: str | None = None

    # ---------------------------------------------------------------- contract

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name=f"intelligence:{self.name}")
        log.info("intelligence_plugin_started", plugin=self.name, interval_seconds=self.interval_seconds)

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        log.info("intelligence_plugin_stopped", plugin=self.name, ticks=self._ticks, published=self._published_count)

    async def health(self) -> PluginHealth:
        if self._last_error:
            return PluginHealth(status="degraded", detail=f"{self._ticks} poll(s); {self._last_error}")
        return PluginHealth(status="healthy", detail=f"{self._ticks} poll(s), {self._published_count} item(s) published")

    def config(self) -> dict[str, Any]:
        return {"interval_seconds": self.interval_seconds}

    def permissions(self) -> list[str]:
        return [PluginPermission.NETWORK_OUTBOUND, PluginPermission.EVENTS_PUBLISH]

    # ---------------------------------------------------------------- polling

    async def _run_loop(self) -> None:
        """Polls forever at ``interval_seconds`` until ``shutdown()``
        cancels this task. A failed poll is logged and retried on the next
        interval — a bad response from one intelligence source never takes
        the rest of the platform down."""
        while True:
            try:
                await self.poll_once()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                log.exception("intelligence_poll_failed", plugin=self.name)
            self._ticks += 1
            await asyncio.sleep(self.interval_seconds)

    async def poll_once(self) -> None:
        """Check for new intelligence and publish it. Override in every
        concrete plugin — the base implementation does nothing, so a
        misconfigured/incomplete plugin degrades to "publishes nothing"
        rather than raising."""
        return

    async def _publish(self, intelligence_event: Event, evidence: Evidence) -> None:
        """Publish the normalized Intelligence Event and its paired
        Evidence Object together — the one piece of behavior every
        intelligence plugin shares, so a plugin can never publish one
        without the other."""
        await self.context.event_bus.publish(intelligence_event)
        await self.context.event_bus.publish(EvidenceProduced(source=self.name, evidence=evidence))
        self._published_count += 1
