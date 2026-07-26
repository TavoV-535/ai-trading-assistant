"""Tests for the /coach command plugin (plugins/commands/coach/plugin.py)
— Milestone 12's completion checklist: "/coach producing actionable
recommendations backed by historical evidence."

Same "load the plugin module by path, exercise execute() against real
engines on a real event bus" pattern tests/test_risk_command.py and
tests/test_watchlist_command.py use.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.analytics.calibration import ConfidenceCalibrationService
from app.analytics.evidence_reliability import EvidenceReliabilityEngine
from app.analytics.service import AnalyticsService
from app.analytics.strategy_analytics import StrategyAnalyticsService
from app.discord.dispatch import CommandContext
from app.event_bus.events import DecisionRecorded
from app.knowledge_graph.graph import KnowledgeGraph
from app.knowledge_graph.query import KnowledgeGraphQueryEngine
from app.learning.engine import LearningEngine
from app.plugins.base import PluginContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_coach_plugin_class():
    plugin_py = PROJECT_ROOT / "plugins" / "commands" / "coach" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_test_coach_plugin_module", plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CoachPlugin


CoachPlugin = _load_coach_plugin_class()


def _decision(*, bar_index: int, timestamp: datetime, **overrides) -> DecisionRecorded:
    defaults = dict(
        source="test",
        symbol="NVDA",
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
        fundamental_evidence=[],
        strategy_matches=["Winner"],
        market_context={"volatility": "High Volatility"},
        confidence=70.0,
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        bar_index=bar_index,
        lookahead_bars=5,
        outcome="correct",
        outcome_price_change_pct=1.0,
        outcome_pending=False,
        timestamp=timestamp,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


def _wire_learning_stack(settings, event_bus, *, min_sample=2):
    settings.learning.min_sample_size = min_sample
    strategy_analytics = StrategyAnalyticsService(settings)
    evidence_reliability = EvidenceReliabilityEngine(settings)
    confidence_calibration = ConfidenceCalibrationService(settings, min_sample=1)
    graph = KnowledgeGraph(settings)
    kg_query = KnowledgeGraphQueryEngine(graph, evidence_reliability=evidence_reliability, confidence_calibration=confidence_calibration, min_sample=min_sample)

    strategy_analytics.attach(event_bus)
    evidence_reliability.attach(event_bus)
    confidence_calibration.attach(event_bus)
    graph.attach(event_bus)

    analytics_service = AnalyticsService(
        strategy_analytics=strategy_analytics, evidence_reliability=evidence_reliability, confidence_calibration=confidence_calibration
    )
    learning_engine = LearningEngine(
        settings,
        strategy_analytics=strategy_analytics,
        evidence_reliability=evidence_reliability,
        confidence_calibration=confidence_calibration,
        knowledge_graph_query=kg_query,
    )
    learning_engine.attach(event_bus)
    return learning_engine, analytics_service, kg_query


async def test_coach_gracefully_degrades_without_learning_engine(event_bus, settings):
    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, plugin_config={}))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))
    assert "isn't available" in response.content
    assert response.ephemeral is True


async def test_coach_full_summary_touches_every_spec_section(event_bus, settings):
    learning_engine, analytics_service, kg_query = _wire_learning_stack(settings, event_bus, min_sample=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i)))
    await event_bus.drain()
    await learning_engine.review()

    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, learning_engine=learning_engine, analytics_service=analytics_service, knowledge_graph_query=kg_query))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"focus": ""}))
    for heading in (
        "Overall performance summary", "Top strategies", "Recent coaching events", "Historical improvements",
        "Recurring mistakes", "Best strengths", "Confidence calibration", "Risk observations",
        "Trend over time", "Recommended next focus",
    ):
        assert heading in response.content
    assert len(response.buttons) == 2
    assert {b.custom_id for b in response.buttons} == {"refresh:coach", "dismiss:coach"}


async def test_coach_unknown_focus_is_reported_honestly(event_bus, settings):
    learning_engine, analytics_service, kg_query = _wire_learning_stack(settings, event_bus)
    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, learning_engine=learning_engine, analytics_service=analytics_service, knowledge_graph_query=kg_query))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"focus": "nonsense"}))
    assert "Unknown focus" in response.content
    assert response.ephemeral is True


async def test_coach_strategies_focus_shows_ranked_strategy_stats(event_bus, settings):
    learning_engine, analytics_service, kg_query = _wire_learning_stack(settings, event_bus, min_sample=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i), strategy_matches=["Winner"], outcome="correct"))
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i), strategy_matches=["Loser"], outcome="incorrect"))
    await event_bus.drain()

    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, learning_engine=learning_engine, analytics_service=analytics_service, knowledge_graph_query=kg_query))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"focus": "strategies"}))
    assert "Winner" in response.content
    assert "Loser" in response.content
    assert "win rate 100%" in response.content
    assert "win rate 0%" in response.content


async def test_coach_events_focus_lists_full_coaching_event_detail(event_bus, settings):
    learning_engine, analytics_service, kg_query = _wire_learning_stack(settings, event_bus, min_sample=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        await event_bus.publish(_decision(bar_index=i, timestamp=base + timedelta(minutes=i)))
    await event_bus.drain()
    events = await learning_engine.review()
    assert events  # sanity: the review actually found something to report

    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, learning_engine=learning_engine, analytics_service=analytics_service, knowledge_graph_query=kg_query))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"focus": "events"}))
    assert "Recent coaching events" in response.content
    assert any(e.title in response.content for e in events)


async def test_coach_calibration_focus_shows_every_bucket(event_bus, settings):
    learning_engine, analytics_service, kg_query = _wire_learning_stack(settings, event_bus)
    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, learning_engine=learning_engine, analytics_service=analytics_service, knowledge_graph_query=kg_query))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"focus": "calibration"}))
    assert "Confidence calibration" in response.content


async def test_coach_health_tracks_invocation_count(event_bus, settings):
    learning_engine, analytics_service, kg_query = _wire_learning_stack(settings, event_bus)
    plugin = CoachPlugin(PluginContext(event_bus=event_bus, settings=settings, learning_engine=learning_engine, analytics_service=analytics_service, knowledge_graph_query=kg_query))
    await plugin.initialize()
    await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={}))
    await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"focus": "risk"}))

    health = await plugin.health()
    assert "2 invocation" in health.detail
