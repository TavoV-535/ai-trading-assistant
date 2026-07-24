"""
Reference External Intelligence Platform plugin: Macro.

Publishes ``MacroEventOccurred`` + a normalized, market-wide (``symbol``
is always ``None``) ``Evidence`` object for simulated macro/economic-
calendar events — Fed meetings, CPI releases, jobs reports, treasury
auctions, and holiday sessions. Same honest-simulation stance as the News
and Earnings reference plugins: no real economic-calendar API is called.

Every macro event this plugin publishes carries
``metadata["context_hint"]`` — the convention the Market Context Engine
(``app/context/engine.py``) reads to promote raw intelligence evidence
into a ``MarketContextUpdated`` label like "Fed Week" or "CPI Day"
without the Context Engine needing to know anything about this specific
plugin. Adding a new macro event type here (a Government Event, a Fed
speech, ...) is a one-line addition to ``_MACRO_EVENTS`` — no other file
in the platform needs to change.
"""
from __future__ import annotations

import random
import zlib
from typing import Any

from app.event_bus.events import MacroEventOccurred
from app.evidence.schema import Evidence, EvidenceCategory
from app.intelligence.plugin import IntelligencePlugin
from app.logging import get_logger

log = get_logger(__name__)


def _stable_seed(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode("utf-8"))


_MACRO_EVENTS: list[dict[str, str]] = [
    {"event_type": "fed_meeting", "title": "Federal Reserve FOMC meeting this week", "context_hint": "fed_week"},
    {"event_type": "cpi_release", "title": "CPI inflation data released", "context_hint": "cpi_day"},
    {"event_type": "jobs_report", "title": "Non-farm payrolls report released", "context_hint": "jobs_report"},
    {
        "event_type": "treasury_auction",
        "title": "Treasury bond auction results released",
        "context_hint": "treasury_auction",
    },
    {
        "event_type": "holiday_session",
        "title": "Markets operating on a shortened holiday session",
        "context_hint": "holiday_session",
    },
]


class MacroPlugin(IntelligencePlugin):
    """Simulated macro/economic-calendar feed. Market-wide by design —
    every event it publishes has ``symbol=None``."""

    name = "Macro"
    version = "0.1.0"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._rng = random.Random(_stable_seed(self.name, "market-wide"))
        self._poll_index = 0

    async def poll_once(self) -> None:
        self._poll_index += 1
        # A quiet poll (nothing macro-relevant happening) is the common
        # case, same as News/Earnings.
        if self._rng.random() > 0.4:
            return

        spec = self._rng.choice(_MACRO_EVENTS)
        direction = self._rng.choices(["bullish", "bearish", "neutral"], weights=[0.3, 0.3, 0.4])[0]
        confidence = round(self._rng.uniform(55.0, 90.0), 2)
        score = round(self._rng.uniform(10.0, 30.0), 2)

        intelligence_event = MacroEventOccurred(
            source=self.name,
            macro_event_type=spec["event_type"],
            title=spec["title"],
            symbol=None,
            metadata={"context_hint": spec["context_hint"]},
        )
        evidence = Evidence(
            source=self.name,
            category=EvidenceCategory.MACRO,
            title=spec["title"],
            score=score,
            confidence=confidence,
            direction=direction,
            symbol=None,
            metadata={"event_type": spec["event_type"], "context_hint": spec["context_hint"]},
        )
        await self._publish(intelligence_event, evidence)
