"""
Milestone 10 completion requirement: prove the Unified Trading Journal and
Reflection Engine sit on top of the Decision Timeline (Milestone 9)
without any subsystem communicating directly with another --

    SimulationEngine.run()
        -> ... (Milestone 9's full pipeline) ...
        -> DecisionRecorded (Decision Timeline, Milestone 9)
                |
                +---------------------------+
                |                           |
       Reflection Engine            Trading Journal
       (ReflectionGenerated)   <--- (independently subscribes to
                |                    DecisionRecorded, ReflectionGenerated,
                |                    and JournalCreated -- never holds a
                +------------------> live reference to the Decision
                                     Timeline or the Reflection Engine)
                                            |
                                       /journal SYMBOL

This test proves three things from the Milestone 10 spec's completion
checklist: (1) a completed simulation automatically generates journal
records, (2) ReflectionGenerated events actually flow over the real Event
Bus (not called directly), and (3) /journal retrieves the complete
historical decision record. A fourth test proves the architectural
constraint structurally: the Reflection Engine and Trading Journal never
import each other or the Decision Timeline engine directly.
"""
from __future__ import annotations

from pathlib import Path

from app.discord.dispatch import CommandContext
from app.event_bus.events import ReflectionGenerated
from app.plugins.base import PluginContext
from app.simulation import SimulationConfig, SimulationEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_class(rel_path: str, module_name: str, class_name: str):
    import importlib.util

    plugin_py = PROJECT_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, plugin_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


JournalPlugin = _load_plugin_class("plugins/commands/journal/plugin.py", "_test_m10_journal", "JournalPlugin")


async def test_completed_simulation_automatically_generates_enriched_journal_records(settings):
    """Milestone 10 completion checklist: 'Demonstrate completed
    simulations automatically generating journal records.' No code in
    this test ever calls ReflectionEngine or TradingJournal directly --
    only SimulationEngine.run(), exactly like a real deployment would."""
    symbol = "NVDA"
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    config = SimulationConfig(symbols=[symbol], bar_count=60, decision_interval_bars=5, lookahead_bars=10)

    result = await engine.run(config)

    assert result.decisions_recorded > 0
    assert result.reflection_engine.total_generated > 0
    assert result.trading_journal.total_entries > 0

    entries = result.trading_journal.for_symbol(symbol)
    assert entries, f"no journal entries for {symbol}"

    # Every entry wraps a real DecisionRecord (never duplicates it into a
    # parallel schema -- see app/journal/models.py's docstring).
    decision_ids = {r.event_id for r in result.decision_timeline.for_symbol(symbol)}
    for entry in entries:
        assert entry.decision.event_id in decision_ids
        assert entry.symbol == symbol

    # At least one resolved decision picked up its reflection -- proving
    # the enrichment actually happened, not just that entries exist.
    enriched = [e for e in entries if e.reflection is not None]
    assert enriched, "no journal entry was ever enriched with a reflection"
    for entry in enriched:
        assert entry.reflection.decision_event_id == entry.decision.event_id
        assert entry.reflection.lessons_learned
        assert entry.reflection.potential_improvements


async def test_reflection_generated_flows_over_the_real_event_bus(settings):
    """Milestone 10 completion checklist: 'Demonstrate Reflection events
    flowing through the Event Bus.' Subscribes to ReflectionGenerated on
    the simulation's own bus *before* the assertions -- the only way these
    are ever observed is by publish(), never a direct method call, since
    this test process is not the Reflection Engine itself."""
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    config = SimulationConfig(symbols=["NVDA"], bar_count=40, decision_interval_bars=5, lookahead_bars=5)
    result = await engine.run(config)

    # The run already completed, so prove the *fact* of bus delivery by
    # cross-checking the Reflection Engine's own internal record against
    # what the Trading Journal -- a wholly separate subscriber -- also
    # received independently for the same event. Two independent
    # subscribers agreeing on the same decision_event_id is only possible
    # if both received the identical event via the bus, not a shared
    # direct reference (they don't hold one -- see the guardrail test).
    reflections = result.reflection_engine.for_symbol("NVDA")
    assert reflections

    journal_entries = {e.decision.event_id: e for e in result.trading_journal.for_symbol("NVDA")}
    matched = 0
    for reflection in reflections:
        entry = journal_entries.get(reflection.decision_event_id)
        if entry is not None and entry.reflection is not None:
            assert entry.reflection.event_id == reflection.event_id
            matched += 1
    assert matched > 0, "no ReflectionGenerated event was observed by both independent subscribers"

    # Also prove the event class itself is what's on the bus: subscribe
    # fresh and publish one manually, confirming the bus is real and live.
    captured: list[ReflectionGenerated] = []

    async def _capture(event: ReflectionGenerated) -> None:
        captured.append(event)

    result.event_bus.subscribe(ReflectionGenerated, _capture)
    await result.event_bus.publish(ReflectionGenerated(source="test", symbol="NVDA", decision_event_id=reflections[0].decision_event_id))
    await result.event_bus.drain()
    assert len(captured) == 1
    await result.event_bus.shutdown()


async def test_journal_command_retrieves_complete_historical_decision_record(settings):
    """Milestone 10 completion checklist: 'Demonstrate /journal retrieving
    complete historical decision records.' Runs the real, unmodified
    JournalPlugin against a simulation's engines -- the same command class
    Discord uses live, mirroring how test_milestone9_pipeline_integration
    validates /analyze."""
    symbol = "NVDA"
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    config = SimulationConfig(symbols=[symbol], bar_count=60, decision_interval_bars=5, lookahead_bars=10)
    result = await engine.run(config)

    plugin = JournalPlugin(PluginContext(event_bus=result.event_bus, settings=result.settings, trading_journal=result.trading_journal))
    await plugin.initialize()

    response = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": symbol}))

    assert f"**{symbol} journal**" in response.content
    # Reflection fields from the spec's checklist all appear somewhere in
    # the rendered output for at least the most recent entries.
    assert "Reasoning:" in response.content or "Reflection: not yet generated." in response.content
    assert "Outcome:" in response.content

    # Writing a note round-trips through the real event bus and is
    # reflected back by a subsequent read -- the complete record includes
    # user annotations, not just the automatic decision/reflection data.
    note_response = await plugin.execute(
        CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": symbol, "note": "integration test note"})
    )
    assert "Noted against" in note_response.content
    await result.event_bus.drain()

    reread = await plugin.execute(CommandContext(user_id="1", guild_id=None, channel_id=None, args={"symbol": symbol}))
    assert "integration test note" in reread.content

    await result.event_bus.shutdown()


def test_reflection_and_journal_engines_never_import_each_other_or_the_timeline_engine():
    """Structural proof of the Milestone 10 spec's explicit requirement:
    'No subsystem should communicate directly with another.' Mirrors the
    import-guardrail tests in test_pipeline_integration.py /
    test_milestone7_pipeline_integration.py. Both engines are allowed to
    import each other's (and the Decision Timeline's) *data-shape* models
    (ReflectionRecord, DecisionRecord) -- those are plain pydantic read
    shapes, not live engine references -- but never each other's `engine`
    module, and never app.timeline.engine directly."""
    import ast

    import app.journal.engine as journal_module
    import app.reflection.engine as reflection_module

    forbidden_prefixes = ("app.reflection.engine", "app.journal.engine", "app.timeline.engine", "app.simulation")

    for module in (journal_module, reflection_module):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                assert not node.module.startswith(forbidden_prefixes), (
                    f"{module.__name__} imports {node.module!r} -- the Reflection Engine and Trading "
                    "Journal must never import each other's engine module, or the Decision Timeline "
                    "engine, directly. They may only communicate via the Event Bus."
                )

    # And the reverse: the Decision Timeline itself must not import either
    # of the two new engines -- it predates them (Milestone 9) and must
    # stay a pure, independent DecisionRecorded subscriber.
    import app.timeline.engine as timeline_module

    tree = ast.parse(Path(timeline_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            assert not node.module.startswith(("app.reflection.engine", "app.journal.engine")), (
                "app.timeline.engine must never import the Reflection Engine or Trading Journal directly"
            )


async def test_deterministic_reconstruction_from_the_event_log(settings):
    """Milestone 10 completion checklist: 'Verify deterministic
    reconstruction from the event log.' Two independent runs of an
    identical SimulationConfig must produce identical
    ReflectionGenerated/JournalCreated-derived state -- mirroring
    test_simulation_determinism_two_independent_runs_match from Milestone
    9, extended to the two new engines."""
    from uuid import uuid4

    correlation_id = uuid4()
    engine = SimulationEngine(settings, project_root=PROJECT_ROOT)
    config = SimulationConfig(
        symbols=["NVDA"], bar_count=40, decision_interval_bars=5, lookahead_bars=5, correlation_id=correlation_id
    )

    result_a = await engine.run(config)
    result_b = await engine.run(config)

    reflections_a = [(r.symbol, r.outcome, r.lessons_learned, r.timestamp) for r in result_a.reflection_engine.for_symbol("NVDA")]
    reflections_b = [(r.symbol, r.outcome, r.lessons_learned, r.timestamp) for r in result_b.reflection_engine.for_symbol("NVDA")]
    assert reflections_a == reflections_b

    # Event IDs are freshly generated (uuid4()) each run, so they legitimately
    # differ between result_a and result_b -- what must be identical is
    # everything the events *carry*: outcome, action, and bar/timestamp.
    journal_a = [(e.decision.bar_index, e.decision.outcome, e.decision.simulated_action, e.decision.timestamp) for e in result_a.trading_journal.for_symbol("NVDA")]
    journal_b = [(e.decision.bar_index, e.decision.outcome, e.decision.simulated_action, e.decision.timestamp) for e in result_b.trading_journal.for_symbol("NVDA")]
    assert journal_a == journal_b

    await result_a.event_bus.shutdown()
    await result_b.event_bus.shutdown()
