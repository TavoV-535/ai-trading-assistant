"""
Tests for the reference Macro intelligence plugin
(plugins/intelligence/macro/plugin.py) — always market-wide (symbol=None).
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.event_bus.events import EvidenceProduced, MacroEventOccurred
from app.plugins.base import PluginContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_macro_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "intelligence" / "macro" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_macro_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MacroPlugin


MacroPlugin = _load_macro_plugin_class()


async def test_macro_plugin_publishes_market_wide_events(event_bus, settings):
    plugin = MacroPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={"interval_seconds": 10}))

    macro_events: list[MacroEventOccurred] = []
    evidence_events: list[EvidenceProduced] = []
    event_bus.subscribe(MacroEventOccurred, lambda e: macro_events.append(e))
    event_bus.subscribe(EvidenceProduced, lambda e: evidence_events.append(e))

    for _ in range(30):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    assert len(macro_events) > 0
    assert len(macro_events) == len(evidence_events)
    for macro_event, ev in zip(macro_events, evidence_events):
        assert macro_event.symbol is None
        assert ev.evidence.symbol is None
        assert ev.evidence.category == "Macro"
        assert macro_event.title == ev.evidence.title
        assert "context_hint" in ev.evidence.metadata
        assert ev.evidence.metadata["context_hint"] == macro_event.metadata["context_hint"]


async def test_macro_plugin_events_use_known_context_hints(event_bus, settings):
    plugin = MacroPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={"interval_seconds": 10}))

    macro_events: list[MacroEventOccurred] = []
    event_bus.subscribe(MacroEventOccurred, lambda e: macro_events.append(e))

    for _ in range(30):
        await plugin.poll_once()
    await asyncio.sleep(0.05)

    known_hints = {"fed_week", "cpi_day", "jobs_report", "treasury_auction", "holiday_session"}
    assert macro_events
    assert all(e.metadata["context_hint"] in known_hints for e in macro_events)
    assert all(e.macro_event_type for e in macro_events)
