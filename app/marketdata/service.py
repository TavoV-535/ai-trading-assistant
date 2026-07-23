"""
The Market Data Abstraction Layer.

Sits between every market data provider (live feeds, replay engines,
historical databases, paper trading feeds, future broker APIs — each a
``MarketDataProviderPlugin``) and the Scanner Engine. The Scanner Engine
only ever calls ``MarketDataService.fetch()`` — it never imports or
references a specific provider. That's what makes "adding a provider never
requires modifying the Scanner Engine" true, and what makes multi-provider
failover possible without touching a single line of scanner code.

Built once, after market data provider plugins have loaded (see
``app/core/bootstrap.py`` — provider plugins load in a first pass, this
service is constructed from the result, and only then do scanners and
everything else load, since scanners need this service and providers
don't need to know it exists).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.indicators.bar import Bar
from app.logging import get_logger
from app.marketdata.provider import MarketDataProviderPlugin

if TYPE_CHECKING:
    from app.plugins.registry import PluginRegistry

log = get_logger(__name__)


class MarketDataService:
    """The single interface anything needing market data queries — never a
    specific provider plugin directly."""

    def __init__(self, settings: Any, plugin_registry: "PluginRegistry") -> None:
        configured = list(settings.market_data.providers)
        available: dict[str, MarketDataProviderPlugin] = {
            p.provider_name: p for p in plugin_registry.plugins.values() if isinstance(p, MarketDataProviderPlugin)
        }

        self._providers: list[MarketDataProviderPlugin] = []
        for provider_name in configured:
            provider = available.get(provider_name)
            if provider is None:
                log.warning("market_data_provider_not_found", provider=provider_name, discovered=list(available))
                continue
            self._providers.append(provider)

        if not self._providers:
            log.warning(
                "market_data_no_providers_available",
                configured=configured,
                discovered=list(available),
            )
        else:
            log.info(
                "market_data_service_ready",
                providers=[p.provider_name for p in self._providers],
            )

    @property
    def providers(self) -> list[MarketDataProviderPlugin]:
        """Configured providers, in priority order. Empty if none of
        ``settings.market_data.providers`` was actually discovered — the
        Scanner Engine degrades gracefully in that case (no data, not a
        crash), same pattern as a missing Anthropic key or Discord token."""
        return list(self._providers)

    async def fetch(self, symbols: list[str], timeframe: str) -> dict[str, Bar]:
        """Ask each configured provider in priority order for whichever
        symbols are still missing, merging results — a symbol already
        found by an earlier (higher-priority) provider is never
        overwritten by a later one.

        This is the "future multi-provider failover" PROJECT.md asks for:
        if the primary provider raises (a live feed's connection is down)
        or simply doesn't have some symbols yet, the next configured
        provider is asked for whatever's still missing, instead of the
        whole fetch failing.
        """
        if not symbols:
            return {}

        remaining = list(dict.fromkeys(symbols))  # de-duplicate, preserve order
        results: dict[str, Bar] = {}

        for provider in self._providers:
            if not remaining:
                break
            try:
                fetched = await provider.fetch(remaining, timeframe)
            except Exception:
                log.exception("market_data_provider_fetch_failed", provider=provider.provider_name)
                continue
            for symbol in remaining:
                bar = fetched.get(symbol)
                if bar is not None:
                    results[symbol] = bar
            remaining = [s for s in remaining if s not in results]

        if remaining:
            log.debug("market_data_no_provider_had_symbol", symbols=remaining, timeframe=timeframe)

        return results
