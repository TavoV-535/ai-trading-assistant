"""
The Market Data Provider contract.

A market data provider is a plugin, exactly like an indicator or a Discord
command — drop a folder under ``plugins/market_data/``, implement
``MarketDataProviderPlugin``, and it's auto-discovered by the same
``PluginRegistry`` used for every other plugin category. The Scanner
Engine never talks to a provider directly; it only ever calls
``MarketDataService.fetch()`` (see ``app/marketdata/service.py``) — that
indirection is what makes "the Scanner Engine never needs modification
when a new data provider is added" true.

PROJECT.md lists the eventual sources this abstraction needs to cover:
live provider feeds, a replay engine, a historical database, a paper
trading feed, and future broker APIs. All of them implement this same
contract, whether they're push-based under the hood (a live websocket
feed should cache the latest tick/bar per symbol internally and answer
``fetch()`` from that cache) or pull-based (a replay engine advancing a
cursor, or a historical database running a query) — from the Scanner
Engine's perspective every provider looks identical.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from app.plugins.base import PluginBase

if TYPE_CHECKING:
    from app.indicators.bar import Bar


class MarketDataProviderPlugin(PluginBase):
    """Base class for every market data provider plugin.

    Adds one method on top of the Universal Plugin Contract: ``fetch()``.
    """

    category: str = "market_data"

    #: Matched against ``settings.market_data.providers`` to decide which
    #: provider(s) ``MarketDataService`` actually uses, in priority order.
    #: Deliberately separate from ``name`` (the Universal Plugin Contract's
    #: registry key) since a deployment may want a stable, human-chosen
    #: provider identifier independent of the Python class's plugin name.
    provider_name: str = "unnamed-provider"

    @abstractmethod
    async def fetch(self, symbols: list[str], timeframe: str) -> dict[str, "Bar"]:
        """Return the latest known bar for as many of ``symbols`` as this
        provider currently has data for.

        A symbol this provider simply doesn't have data for yet is omitted
        from the result — that's a normal, expected outcome, never an
        error. Only raise for a genuine provider failure (a live feed's
        connection dropping, a query timing out, ...); ``MarketDataService``
        treats an exception as "this provider is unavailable right now" and
        fails over to the next configured provider rather than letting one
        provider's outage take down the whole fetch.
        """
