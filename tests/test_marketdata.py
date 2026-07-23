"""
Tests for the Market Data Abstraction Layer (app/marketdata/) and the
reference ReplayProviderPlugin (plugins/market_data/replay/).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.indicators.bar import Bar
from app.marketdata.provider import MarketDataProviderPlugin
from app.marketdata.service import MarketDataService
from app.plugins.base import PluginContext, PluginHealth

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_replay_provider_class():
    plugin_py = PROJECT_ROOT / "plugins" / "market_data" / "replay" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_replay_provider_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ReplayProviderPlugin


ReplayProviderPlugin = _load_replay_provider_class()


class _FakeRegistry:
    """Minimal duck-typed stand-in for PluginRegistry -- MarketDataService
    only ever reads ``.plugins``."""

    def __init__(self, plugins: dict) -> None:
        self.plugins = plugins


class _StubProvider(MarketDataProviderPlugin):
    name = "StubProvider"
    provider_name = "stub"

    def __init__(self, context, bars: dict[str, Bar] | None = None, *, fail: bool = False) -> None:
        super().__init__(context)
        self._bars = bars or {}
        self._fail = fail
        self.calls: list[tuple[list[str], str]] = []

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def config(self) -> dict:
        return {}

    def permissions(self) -> list:
        return []

    async def fetch(self, symbols, timeframe):
        self.calls.append((list(symbols), timeframe))
        if self._fail:
            raise RuntimeError("simulated provider failure")
        return {s: b for s, b in self._bars.items() if s in symbols}


def _bar(close: float) -> Bar:
    return Bar(open=close, high=close, low=close, close=close, volume=100)


def _context(event_bus, settings, plugin_config=None) -> PluginContext:
    return PluginContext(event_bus=event_bus, settings=settings, plugin_config=plugin_config or {})


# ---------------------------------------------------------------- MarketDataService


async def test_service_uses_only_configured_providers_in_order(event_bus, settings):
    settings.market_data.providers = ["primary", "backup"]
    primary = _StubProvider(_context(event_bus, settings), bars={"NVDA": _bar(100)})
    backup = _StubProvider(_context(event_bus, settings), bars={"NVDA": _bar(200), "AAPL": _bar(50)})
    primary.provider_name = "primary"
    backup.provider_name = "backup"

    registry = _FakeRegistry({"Primary": primary, "Backup": backup})
    service = MarketDataService(settings, registry)

    result = await service.fetch(["NVDA", "AAPL"], "1m")

    # NVDA came from "primary" (higher priority) even though "backup" also has it
    assert result["NVDA"].close == 100
    # AAPL only exists on "backup" -- failover found it
    assert result["AAPL"].close == 50
    assert primary.calls == [(["NVDA", "AAPL"], "1m")]
    assert backup.calls == [(["AAPL"], "1m")]  # only asked for what was still missing


async def test_service_fails_over_when_a_provider_raises(event_bus, settings):
    settings.market_data.providers = ["broken", "ok"]
    broken = _StubProvider(_context(event_bus, settings), fail=True)
    ok = _StubProvider(_context(event_bus, settings), bars={"NVDA": _bar(123)})
    broken.provider_name = "broken"
    ok.provider_name = "ok"

    registry = _FakeRegistry({"Broken": broken, "Ok": ok})
    service = MarketDataService(settings, registry)

    result = await service.fetch(["NVDA"], "1m")
    assert result["NVDA"].close == 123


async def test_service_omits_symbols_no_provider_has(event_bus, settings):
    settings.market_data.providers = ["only"]
    only = _StubProvider(_context(event_bus, settings), bars={"NVDA": _bar(10)})
    only.provider_name = "only"

    registry = _FakeRegistry({"Only": only})
    service = MarketDataService(settings, registry)

    result = await service.fetch(["NVDA", "GHOST"], "1m")
    assert "NVDA" in result
    assert "GHOST" not in result


async def test_service_returns_empty_dict_for_empty_symbol_list(event_bus, settings):
    registry = _FakeRegistry({})
    service = MarketDataService(settings, registry)
    assert await service.fetch([], "1m") == {}


async def test_service_degrades_gracefully_with_no_providers_discovered(event_bus, settings):
    settings.market_data.providers = ["nonexistent"]
    registry = _FakeRegistry({})
    service = MarketDataService(settings, registry)  # must not raise
    assert service.providers == []
    assert await service.fetch(["NVDA"], "1m") == {}


async def test_service_ignores_non_provider_plugins(event_bus, settings):
    """Something in the registry that isn't a MarketDataProviderPlugin
    (e.g. an indicator) must never be mistaken for a provider."""

    class _NotAProvider:
        provider_name = "sneaky"

    settings.market_data.providers = ["sneaky"]
    registry = _FakeRegistry({"NotAProvider": _NotAProvider()})
    service = MarketDataService(settings, registry)
    assert service.providers == []


# ---------------------------------------------------------------- ReplayProviderPlugin


async def test_replay_provider_synthetic_walk_is_deterministic(event_bus, settings):
    ctx1 = _context(event_bus, settings, {"seed": 42, "start_price": 50.0})
    ctx2 = _context(event_bus, settings, {"seed": 42, "start_price": 50.0})
    p1 = ReplayProviderPlugin(ctx1)
    p2 = ReplayProviderPlugin(ctx2)
    await p1.initialize()
    await p2.initialize()

    closes1 = [(await p1.fetch(["NVDA"], "1m"))["NVDA"].close for _ in range(5)]
    closes2 = [(await p2.fetch(["NVDA"], "1m"))["NVDA"].close for _ in range(5)]

    assert closes1 == closes2  # same seed -> identical sequence
    assert len(set(closes1)) > 1  # actually varies, not a constant


async def test_replay_provider_different_symbols_walk_independently(event_bus, settings):
    ctx = _context(event_bus, settings, {"seed": 7})
    provider = ReplayProviderPlugin(ctx)
    await provider.initialize()

    result = await provider.fetch(["NVDA", "AAPL"], "1m")
    assert result["NVDA"].close != result["AAPL"].close


async def test_replay_provider_reads_csv_when_available(event_bus, settings, tmp_path):
    csv_path = tmp_path / "NVDA.csv"
    csv_path.write_text("open,high,low,close,volume\n100,101,99,100.5,1000\n102,103,101,102.5,1100\n")

    ctx = _context(event_bus, settings, {"data_dir": str(tmp_path)})
    provider = ReplayProviderPlugin(ctx)
    await provider.initialize()

    first = (await provider.fetch(["NVDA"], "1m"))["NVDA"]
    second = (await provider.fetch(["NVDA"], "1m"))["NVDA"]
    third = (await provider.fetch(["NVDA"], "1m"))["NVDA"]  # loops back to the first row

    assert first.close == 100.5
    assert second.close == 102.5
    assert third.close == 100.5


async def test_replay_provider_falls_back_to_synthetic_for_symbol_without_csv(event_bus, settings, tmp_path):
    (tmp_path / "NVDA.csv").write_text("open,high,low,close,volume\n100,101,99,100.5,1000\n")

    ctx = _context(event_bus, settings, {"data_dir": str(tmp_path)})
    provider = ReplayProviderPlugin(ctx)
    await provider.initialize()

    result = await provider.fetch(["NVDA", "GHOST"], "1m")
    assert result["NVDA"].close == 100.5
    assert "GHOST" in result  # synthetic fallback, not omitted


async def test_replay_provider_health_and_config(event_bus, settings):
    ctx = _context(event_bus, settings, {"seed": 1})
    provider = ReplayProviderPlugin(ctx)
    await provider.initialize()
    await provider.fetch(["NVDA"], "1m")

    health = await provider.health()
    assert health.status == "healthy"
    assert "1 fetch" in health.detail

    assert provider.config()["seed"] == 1
    assert provider.permissions()
