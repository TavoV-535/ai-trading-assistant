"""
Reference External Intelligence Platform plugin: News.

Publishes ``NewsReceived`` + a normalized ``Evidence`` object for each
simulated headline it generates against the configured watchlist —
exactly the same (event, evidence) pairing every intelligence plugin
publishes (see ``app/intelligence/plugin.py``). Nothing here is special-
cased anywhere else in the platform; this plugin only exists because
``plugins/intelligence/news/`` is a folder that implements
``IntelligencePlugin``.

HONEST LIMITATION: no real news API is called — this sandbox has no
network access to one, and PROJECT.md's development requirements ask for
honesty over fabricated realism (the same stance ``ReplayProviderPlugin``
took for synthetic price data in Milestone 6). Headlines are generated
from a small template set, deterministically per ``(symbol, plugin name)``
via a stable seed, and every published event's ``provider`` field says
``"synthetic-news-feed"`` so it's never mistaken for real news. Swapping
in a real provider (NewsAPI, Benzinga, Polygon's news endpoint, ...) is a
new plugin against this exact same contract — zero changes anywhere else.
"""
from __future__ import annotations

import random
import zlib
from typing import Any

from app.event_bus.events import NewsReceived
from app.evidence.schema import Evidence, EvidenceCategory
from app.intelligence.plugin import IntelligencePlugin
from app.logging import get_logger

log = get_logger(__name__)

_HEADLINES: dict[str, list[str]] = {
    "bullish": [
        "{symbol} shares rise on analyst upgrade",
        "{symbol} announces expanded product line, investors optimistic",
        "{symbol} tops preliminary revenue expectations",
        "Institutional buying reported in {symbol}",
    ],
    "bearish": [
        "{symbol} shares fall on analyst downgrade",
        "{symbol} flags supply chain disruption",
        "{symbol} guidance revised lower ahead of earnings",
        "Regulatory scrutiny weighs on {symbol}",
    ],
    "neutral": [
        "{symbol} announces routine executive appointment",
        "{symbol} to present at upcoming industry conference",
        "{symbol} completes previously announced buyback tranche",
    ],
}


def _stable_seed(*parts: str) -> int:
    """Deterministic per-process-independent seed — ``hash()`` is
    randomized per Python process, which would make this plugin's output
    (and its tests) non-reproducible run to run."""
    return zlib.crc32("|".join(parts).encode("utf-8"))


class NewsPlugin(IntelligencePlugin):
    """Simulated news headline feed for the configured watchlist."""

    name = "News"
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
        for symbol in self.watchlist:
            rng = self._rng_by_symbol.setdefault(symbol, random.Random(_stable_seed(self.name, symbol)))
            # A real feed doesn't publish on a fixed cadence either -- most
            # polls turn up nothing new.
            if rng.random() > 0.5:
                continue

            sentiment = rng.choices(["bullish", "bearish", "neutral"], weights=[0.4, 0.35, 0.25])[0]
            headline = rng.choice(_HEADLINES[sentiment]).format(symbol=symbol)
            score = round(rng.uniform(5, 20), 2)
            confidence = round(rng.uniform(40, 75), 2)

            intelligence_event = NewsReceived(
                source=self.name,
                headline=headline,
                symbol=symbol,
                provider="synthetic-news-feed",
                sentiment=sentiment,
            )
            evidence = Evidence(
                source=self.name,
                category=EvidenceCategory.NEWS,
                title=headline,
                score=score,
                confidence=confidence,
                direction=sentiment,
                symbol=symbol,
                metadata={"provider": "synthetic-news-feed", "poll_index": self._poll_index},
            )
            await self._publish(intelligence_event, evidence)
