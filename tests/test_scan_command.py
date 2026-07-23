"""
Tests for the /scan command plugin (plugins/commands/scan/plugin.py).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.discord.dispatch import CommandContext
from app.marketdata.service import MarketDataService
from app.plugins.base import PluginContext
from app.plugins.registry import PluginRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_scan_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "commands" / "scan" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_scan_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ScanStatusPlugin


ScanStatusPlugin = _load_scan_plugin_class()


async def test_scan_reports_no_scanners_loaded(event_bus, settings):
    registry = PluginRegistry(event_bus, settings)
    plugin = ScanStatusPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, plugin_registry=registry)
    )
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await plugin.execute(ctx)

    assert "No scanners are currently loaded" in response.content
    labels = [b.label for b in response.buttons]
    assert labels == ["Refresh", "Dismiss"]


async def test_scan_reports_loaded_scanners_and_health(event_bus, settings, tmp_path):
    from app.scanner.plugin import ScannerPlugin

    registry = PluginRegistry(event_bus, settings)

    class _FakeMarketData:
        async def fetch(self, symbols, timeframe):
            return {}

    scanner_ctx = PluginContext(
        event_bus=event_bus,
        settings=settings,
        plugin_config={"watchlist": ["NVDA", "AAPL"], "timeframes": ["1m"], "interval_seconds": 10},
        market_data_service=_FakeMarketData(),
    )
    scanner = ScannerPlugin(scanner_ctx)
    scanner.name = "TestScanner"
    await scanner.initialize()
    registry._plugins[scanner.name] = scanner  # test-only direct injection

    plugin = ScanStatusPlugin(
        PluginContext(event_bus=event_bus, settings=settings, plugin_config={}, plugin_registry=registry)
    )
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await plugin.execute(ctx)

    assert "TestScanner" in response.content
    assert "NVDA, AAPL" in response.content
    assert "1m" in response.content
    assert "10s" in response.content

    await scanner.shutdown()
    await registry.shutdown_all()


async def test_scan_reports_market_data_providers(event_bus, settings):
    class _FakeRegistry:
        plugins: dict = {}

    market_data = MarketDataService(settings, _FakeRegistry())  # no providers discovered
    registry = PluginRegistry(event_bus, settings)

    plugin = ScanStatusPlugin(
        PluginContext(
            event_bus=event_bus,
            settings=settings,
            plugin_config={},
            plugin_registry=registry,
            market_data_service=market_data,
        )
    )
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await plugin.execute(ctx)

    assert "Market data provider(s): none configured" in response.content


async def test_scan_degrades_gracefully_without_plugin_registry(event_bus, settings):
    plugin = ScanStatusPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()

    ctx = CommandContext(user_id="1", guild_id=None, channel_id=None, args={})
    response = await plugin.execute(ctx)

    assert "isn't available" in response.content
    assert response.ephemeral is True
