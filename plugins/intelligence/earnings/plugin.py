"""
Reference External Intelligence Platform plugin: Earnings.

Publishes ``EarningsReleased`` + a normalized ``Evidence`` object for each
simulated earnings report against the configured watchlist. Same honest-
simulation stance as ``plugins/intelligence/news/`` — no real earnings-
calendar API is called; figures are generated deterministically per
symbol via a stable seed and every published evidence's metadata carries
the exact eps/surprise numbers used, so nothing here hides how the number
was produced.

One small piece of real derived behavior: if more than one symbol in the
watchlist reports "earnings" within the same poll cycle, the evidence for
those reports carries ``metadata["context_hint"] = "earnings_season"`` —
a defensible, if simple, proxy for "several companies reporting around
the same time" that the Market Context Engine (``app/context/engine.py``)
promotes into an "Earnings Season" ``MarketContextUpdated`` event.
"""
from __future__ import annotations

import random
import zlib
from typing import Any

from app.event_bus.events import EarningsReleased
from app.evidence.schema import Evidence, EvidenceCategory
from app.intelligence.plugin import IntelligencePlugin
from app.logging import get_logger

log = get_logger(__name__)


def _stable_seed(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode("utf-8"))


class EarningsPlugin(IntelligencePlugin):
    """Simulated earnings-release feed for the configured watchlist."""

    name = "Earnings"
    version = "0.1.0"

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        cfg = context.plugin_config
        self.watchlist: tuple[str, ...] = tuple(cfg.get("watchlist") or [])
        self._rng_by_symbol: dict[str, random.Random] = {}
        self._poll_index = 0

    def config(self) -> dict[str, Any]:
        cfg = super().config()
        cfg["watchlist"] = list(self.watchlist)
        return cfg

    async def poll_once(self) -> None:
        self._poll_index += 1
        # Earnings are much rarer events than headlines -- gather every
        # candidate release for this poll first so the earnings-season
        # heuristic below can see how many symbols reported together.
        releases: list[tuple[str, float, float, float]] = []
        for symbol in self.watchlist:
            rng = self._rng_by_symbol.setdefault(symbol, random.Random(_stable_seed(self.name, symbol)))
            if rng.random() > 0.25:
                continue
            eps_estimate = round(rng.uniform(0.5, 5.0), 2)
            surprise_percent = round(rng.uniform(-25.0, 25.0), 2)
            revenue_estimate = round(rng.uniform(500.0, 5000.0), 1)
            releases.append((symbol, eps_estimate, surprise_percent, revenue_estimate))

        is_earnings_season = len(releases) >= 2 and len(self.watchlist) > 1

        for symbol, eps_estimate, surprise_percent, revenue_estimate in releases:
            eps_actual = round(eps_estimate * (1 + surprise_percent / 100), 2)
            revenue_actual = round(revenue_estimate * (1 + surprise_percent / 200), 1)

            if surprise_percent >= 5:
                direction = "bullish"
                title = f"{symbol} beats EPS estimate by {surprise_percent:.1f}%"
            elif surprise_percent <= -5:
                direction = "bearish"
                title = f"{symbol} misses EPS estimate by {abs(surprise_percent):.1f}%"
            else:
                direction = "neutral"
                title = f"{symbol} reports EPS in line with estimates"

            intelligence_event = EarningsReleased(
                source=self.name,
                symbol=symbol,
                eps_actual=eps_actual,
                eps_estimate=eps_estimate,
                revenue_actual=revenue_actual,
                revenue_estimate=revenue_estimate,
                surprise_percent=surprise_percent,
            )
            metadata: dict[str, Any] = {
                "eps_actual": eps_actual,
                "eps_estimate": eps_estimate,
                "surprise_percent": surprise_percent,
            }
            if is_earnings_season:
                metadata["context_hint"] = "earnings_season"

            evidence = Evidence(
                source=self.name,
                category=EvidenceCategory.EARNINGS,
                title=title,
                score=round(abs(surprise_percent), 2),
                confidence=round(min(95.0, 60 + abs(surprise_percent)), 2),
                direction=direction,
                symbol=symbol,
                metadata=metadata,
            )
            await self._publish(intelligence_event, evidence)
