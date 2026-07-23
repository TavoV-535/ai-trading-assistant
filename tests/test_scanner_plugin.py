"""
Tests for the Scanner Plugin contract (app/scanner/plugin.py).

Drives ``ScannerPlugin`` directly with a short ``interval_seconds`` rather
than relying on the reference ``CoreWatchlistScanner``'s 5s default —
short intervals keep this file fast and deterministic. See
tests/test_scanner_pipeline_integration.py for the full continuous-
scanning-to-/analyze demonstration using real indicator plugins.
"""
from __future__ import annotations

import asyncio

from app.event_bus.events import MarketDataUpdated
from app.indicators.bar import Bar
from app.plugins.base import PluginContext
from app.scanner.plugin import ScannerPlugin


class _FakeMarketDataService:
    def __init__(self, bars_by_symbol: dict[str, Bar] | None = None, *, fail: bool = False) -> None:
        self._bars = bars_by_symbol or {}
        self.calls: list[tuple[list[str], str]] = []
        self._fail = fail

    async def fetch(self, symbols, timeframe):
        self.calls.append((list(symbols), timeframe))
        if self._fail:
            raise RuntimeError("simulated market data failure")
        return dict(self._bars)


def _bar(close: float) -> Bar:
    return Bar(open=close, high=close, low=close, close=close, volume=10)


def _context(event_bus, settings, *, plugin_config=None, market_data_service=None) -> PluginContext:
    return PluginContext(
        event_bus=event_bus,
        settings=settings,
        plugin_config=plugin_config or {},
        market_data_service=market_data_service,
    )


async def test_scan_once_publishes_market_data_updated_per_symbol(event_bus, settings):
    market_data = _FakeMarketDataService({"NVDA": _bar(100), "AAPL": _bar(50)})
    ctx = _context(
        event_bus, settings,
        plugin_config={"watchlist": ["NVDA", "AAPL"], "timeframes": ["1m"]},
        market_data_service=market_data,
    )
    scanner = ScannerPlugin(ctx)

    received: list[MarketDataUpdated] = []

    async def on_market_data(e: MarketDataUpdated) -> None:
        received.append(e)

    event_bus.subscribe(MarketDataUpdated, on_market_data)

    await scanner.scan_once()
    await asyncio.sleep(0.05)

    assert {e.symbol for e in received} == {"NVDA", "AAPL"}
    assert all(e.timeframe == "1m" for e in received)
    assert all(e.source == scanner.name for e in received)


async def test_scan_once_covers_every_configured_timeframe(event_bus, settings):
    market_data = _FakeMarketDataService({"NVDA": _bar(100)})
    ctx = _context(
        event_bus, settings,
        plugin_config={"watchlist": ["NVDA"], "timeframes": ["1m", "5m"]},
        market_data_service=market_data,
    )
    scanner = ScannerPlugin(ctx)

    received: list[MarketDataUpdated] = []
    event_bus.subscribe(MarketDataUpdated, lambda e: received.append(e))
    await scanner.scan_once()
    await asyncio.sleep(0.05)

    assert sorted(e.timeframe for e in received) == ["1m", "5m"]


async def test_scan_once_never_calls_an_indicator_plugin_directly():
    """Structural guarantee: app/scanner/plugin.py only imports the Event
    Bus / evidence / logging / its own module -- never a specific
    indicator plugin, mirroring the same check already applied to the
    Strategy Engine (tests/test_pipeline_integration.py)."""
    import ast
    from pathlib import Path

    import app.scanner.plugin as scanner_module

    allowed_prefixes = ("app.event_bus", "app.plugins", "app.logging", "app.scanner")
    tree = ast.parse(Path(scanner_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            assert node.module.startswith(allowed_prefixes), (
                f"app.scanner.plugin imports {node.module!r} -- the Scanner Engine "
                "must never import a specific indicator/plugin module"
            )


async def test_scan_once_gracefully_skips_without_market_data_service(event_bus, settings):
    ctx = _context(event_bus, settings, plugin_config={"watchlist": ["NVDA"]})
    scanner = ScannerPlugin(ctx)

    await scanner.scan_once()  # must not raise

    health = await scanner.health()
    assert health.status == "degraded"
    assert "not configured" in health.detail


async def test_scan_once_does_nothing_with_empty_watchlist(event_bus, settings):
    market_data = _FakeMarketDataService({"NVDA": _bar(1)})
    ctx = _context(event_bus, settings, market_data_service=market_data)
    scanner = ScannerPlugin(ctx)

    await scanner.scan_once()

    assert market_data.calls == []


async def test_initialize_starts_a_background_loop_that_ticks_repeatedly(event_bus, settings):
    market_data = _FakeMarketDataService({"NVDA": _bar(1)})
    ctx = _context(
        event_bus, settings,
        plugin_config={"watchlist": ["NVDA"], "timeframes": ["1m"], "interval_seconds": 0.02},
        market_data_service=market_data,
    )
    scanner = ScannerPlugin(ctx)

    await scanner.initialize()
    await asyncio.sleep(0.15)  # several intervals' worth
    await scanner.shutdown()

    assert len(market_data.calls) >= 3
    health = await scanner.health()
    assert health.status == "healthy"


async def test_initialize_with_empty_watchlist_reports_degraded_immediately(event_bus, settings):
    ctx = _context(event_bus, settings, plugin_config={"interval_seconds": 10})
    scanner = ScannerPlugin(ctx)

    await scanner.initialize()
    health = await scanner.health()
    await scanner.shutdown()

    assert health.status == "degraded"
    assert "empty watchlist" in health.detail


async def test_a_failing_tick_is_isolated_and_retried(event_bus, settings):
    market_data = _FakeMarketDataService(fail=True)
    ctx = _context(
        event_bus, settings,
        plugin_config={"watchlist": ["NVDA"], "interval_seconds": 0.02},
        market_data_service=market_data,
    )
    scanner = ScannerPlugin(ctx)

    await scanner.initialize()
    await asyncio.sleep(0.1)
    await scanner.shutdown()  # must not raise / crash the loop

    health = await scanner.health()
    assert health.status == "degraded"
    assert "simulated market data failure" in health.detail


async def test_config_and_permissions():
    from app.plugins.base import PluginPermission

    class _DummyEventBus:
        pass

    ctx = PluginContext(
        event_bus=_DummyEventBus(),
        settings=type("S", (), {"scanner": type("Sc", (), {"timeframes": ["1m"], "interval_seconds": 60})()})(),
        plugin_config={"watchlist": ["NVDA"], "interval_seconds": 30, "asset_class": "crypto"},
    )
    scanner = ScannerPlugin(ctx)

    assert scanner.config() == {
        "watchlist": ["NVDA"],
        "timeframes": ["1m"],
        "interval_seconds": 30.0,
        "asset_class": "crypto",
    }
    assert PluginPermission.MARKET_DATA_READ in scanner.permissions()
    assert PluginPermission.EVENTS_PUBLISH in scanner.permissions()
