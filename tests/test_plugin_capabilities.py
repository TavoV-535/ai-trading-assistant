"""Unit tests for Milestone 12's plugin capability metadata
(``PluginCapabilities`` on ``app/plugins/base.py``, plus
``PluginRegistry.capabilities_all()``/``.supporting()``)."""
from __future__ import annotations

from app.event_bus.bus import EventBus
from app.plugins.base import PluginBase, PluginCapabilities, PluginContext, PluginHealth
from app.plugins.registry import PluginRegistry


class _DefaultPlugin(PluginBase):
    name = "default-plugin"

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth()

    def config(self) -> dict:
        return {}

    def permissions(self) -> list:
        return []


class _CryptoPlugin(_DefaultPlugin):
    name = "crypto-plugin"

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            evidence_types=["Trend"], market_types=["crypto"], symbols=["BTC-USD"], timeframes=["1h"]
        )


def _context() -> PluginContext:
    return PluginContext(event_bus=EventBus(), settings=object())


# ---------------------------------------------------------------- PluginCapabilities.supports()


def test_default_capabilities_are_fully_unspecified_and_match_everything():
    plugin = _DefaultPlugin(_context())
    caps = plugin.capabilities()
    assert caps == PluginCapabilities()
    assert caps.supports(evidence_type="News", market_type="forex", symbol="AAPL", timeframe="1d") is True


def test_declared_capabilities_narrow_what_matches():
    plugin = _CryptoPlugin(_context())
    caps = plugin.capabilities()
    assert caps.supports(market_type="crypto") is True
    assert caps.supports(market_type="equity") is False
    assert caps.supports(symbol="BTC-USD") is True
    assert caps.supports(symbol="AAPL") is False
    assert caps.supports(timeframe="1h") is True
    assert caps.supports(timeframe="5m") is False
    # Every declared field must match simultaneously.
    assert caps.supports(market_type="crypto", symbol="AAPL") is False


def test_supports_treats_omitted_criteria_as_unconstrained():
    plugin = _CryptoPlugin(_context())
    caps = plugin.capabilities()
    # No symbol/timeframe/evidence_type given -- only market_type checked.
    assert caps.supports(market_type="crypto") is True


# ---------------------------------------------------------------- PluginRegistry.capabilities_all() / .supporting()


def _registry_with(*plugins: PluginBase) -> PluginRegistry:
    registry = PluginRegistry(EventBus(), object())
    registry._plugins = {p.name: p for p in plugins}
    return registry


def test_capabilities_all_reports_every_loaded_plugin():
    ctx = _context()
    registry = _registry_with(_DefaultPlugin(ctx), _CryptoPlugin(ctx))
    all_caps = registry.capabilities_all()
    assert set(all_caps.keys()) == {"default-plugin", "crypto-plugin"}
    assert all_caps["crypto-plugin"].market_types == ["crypto"]


def test_supporting_filters_by_every_criterion_given():
    ctx = _context()
    registry = _registry_with(_DefaultPlugin(ctx), _CryptoPlugin(ctx))

    crypto_capable = registry.supporting(market_type="crypto")
    assert {p.name for p in crypto_capable} == {"default-plugin", "crypto-plugin"}  # unspecified plugin matches too

    equity_capable = registry.supporting(market_type="equity")
    assert {p.name for p in equity_capable} == {"default-plugin"}  # crypto-plugin explicitly excludes equity


def test_supporting_with_no_criteria_returns_every_plugin():
    ctx = _context()
    registry = _registry_with(_DefaultPlugin(ctx), _CryptoPlugin(ctx))
    assert len(registry.supporting()) == 2
