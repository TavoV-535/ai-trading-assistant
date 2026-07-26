"""Unit tests for the Trading Knowledge Graph and its Explainable Query
Layer (``app/knowledge_graph/``) — Milestone 12."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.event_bus.bus import EventBus
from app.event_bus.events import (
    CoachingEvent,
    DecisionRecorded,
    JournalCreated,
    MarketContextUpdated,
    ReflectionGenerated,
    RiskEvent,
    StrategyMatched,
)
from app.knowledge_graph.graph import KnowledgeGraph
from app.knowledge_graph.models import NodeType
from app.knowledge_graph.query import KnowledgeGraphQueryEngine


def _decision(symbol="NVDA", **overrides) -> DecisionRecorded:
    defaults = dict(
        source="test",
        symbol=symbol,
        market_context={"trend": "Bull Trend"},
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
        fundamental_evidence=[],
        strategy_matches=["Momentum Breakout"],
        reasoning_summary="Bullish evidence dominates.",
        confidence=70.0,
        simulated_action="watch_bullish",
        price_at_decision=100.0,
        bar_index=0,
        lookahead_bars=5,
        outcome="correct",
        outcome_price_change_pct=2.0,
        outcome_pending=False,
    )
    defaults.update(overrides)
    return DecisionRecorded(**defaults)


# ---------------------------------------------------------------- KnowledgeGraph


async def test_decision_recorded_creates_symbol_and_decision_hub_nodes(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    await bus.publish(_decision())
    await bus.drain()

    symbol_node = graph.node(f"{NodeType.SYMBOL}:NVDA")
    assert symbol_node is not None
    decisions = graph.nodes_by_type(NodeType.DECISION)
    assert len(decisions) == 1
    edges = graph.edges_from(symbol_node.node_id, relation="has_decision")
    assert len(edges) == 1
    assert edges[0].target_id == decisions[0].node_id
    await bus.shutdown()


async def test_decision_hub_links_strategy_evidence_context_and_outcome(settings):
    """Strategy/Evidence/MarketContext all connect through the Decision
    node, never directly to each other -- the spec's example chain
    (Strategy -> Evidence -> MarketContext -> RiskProfile -> Outcome) is a
    2-hop walk through this hub."""
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    await bus.publish(RiskEvent(source="test", symbol="NVDA", risk_type="daily_drawdown", severity="info", value=0.0, profile_name="swing_trader", message="ok"))
    await bus.drain()
    await bus.publish(_decision())
    await bus.drain()

    decision = graph.nodes_by_type(NodeType.DECISION)[0]
    strategy_neighbors = graph.neighbors(decision.node_id, relation="matched_strategy")
    assert [n.node_type for n in strategy_neighbors] == [NodeType.STRATEGY]

    evidence_neighbors = graph.neighbors(decision.node_id, relation="considered_evidence")
    assert len(evidence_neighbors) == 1
    assert evidence_neighbors[0].node_type == NodeType.EVIDENCE

    context_neighbors = graph.neighbors(decision.node_id, relation="occurred_during")
    assert len(context_neighbors) == 1
    assert context_neighbors[0].node_type == NodeType.MARKET_CONTEXT

    risk_neighbors = graph.neighbors(decision.node_id, relation="evaluated_under")
    assert len(risk_neighbors) == 1
    assert risk_neighbors[0].label == "swing_trader"

    outcome_neighbors = graph.neighbors(decision.node_id, relation="resulted_in")
    assert len(outcome_neighbors) == 1
    assert outcome_neighbors[0].node_type == NodeType.OUTCOME
    await bus.shutdown()


async def test_repeated_strategy_match_dedupes_edges_not_nodes(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    for _ in range(3):
        await bus.publish(StrategyMatched(source="test", strategy="Momentum Breakout", symbol="NVDA", score=10.0))
    await bus.drain()

    strategies = graph.nodes_by_type(NodeType.STRATEGY)
    assert len(strategies) == 1
    edges = graph.edges_from(f"{NodeType.SYMBOL}:NVDA", relation="matched")
    assert len(edges) == 1  # deduped, not tripled
    await bus.shutdown()


async def test_reflection_journal_and_coaching_events_attach_to_the_graph(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    decision_event = _decision()
    await bus.publish(decision_event)
    await bus.drain()

    await bus.publish(ReflectionGenerated(source="test", symbol="NVDA", decision_event_id=decision_event.event_id, reasoning="r", lessons_learned="Entry timing was good."))
    await bus.publish(JournalCreated(source="test", symbol="NVDA", decision_event_id=decision_event.event_id, note="Felt good about this one."))
    await bus.publish(MarketContextUpdated(source="test", symbol="NVDA", context_type="volatility", label="High Volatility"))
    await bus.publish(CoachingEvent(source="test", pattern_type="strongest_strategy", title="Momentum Breakout is strongest", symbol="NVDA", related_strategies=["Momentum Breakout"]))
    await bus.drain()

    decision_id = f"{NodeType.DECISION}:{decision_event.event_id}"
    assert len(graph.edges_from(decision_id, relation="reflected_by")) == 1
    assert len(graph.edges_from(decision_id, relation="annotated_by")) == 1
    assert len(graph.nodes_by_type(NodeType.MARKET_REGIME)) >= 1
    coaching_nodes = graph.nodes_by_type("coaching")
    assert len(coaching_nodes) == 1
    assert coaching_nodes[0].attributes["pattern_type"] == "strongest_strategy"
    await bus.shutdown()


def test_max_edges_evicts_oldest_edge(settings):
    graph = KnowledgeGraph(settings, max_edges=2)
    # Directly exercise the private upsert/add-edge path with distinct
    # relations so nothing dedupes -- this test only cares about eviction.
    n1 = graph._upsert_node(NodeType.SYMBOL, "A", "A")
    n2 = graph._upsert_node(NodeType.SYMBOL, "B", "B")
    n3 = graph._upsert_node(NodeType.SYMBOL, "C", "C")
    graph._add_edge(n1.node_id, n2.node_id, "rel1")
    graph._add_edge(n1.node_id, n3.node_id, "rel2")
    graph._add_edge(n2.node_id, n3.node_id, "rel3")  # pushes total to 3 > max_edges=2

    assert graph.statistics()["total_edges"] == 2
    assert graph.edges_from(n1.node_id, relation="rel1") == []  # oldest, evicted
    assert len(graph.edges_from(n1.node_id, relation="rel2")) == 1


async def test_health_diagnostics_statistics_shapes(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)
    await bus.publish(_decision())
    await bus.drain()

    health = await graph.health()
    assert health["status"] == "healthy"
    assert health["nodes"] > 0

    diagnostics = graph.diagnostics()
    assert diagnostics["total_events_observed"] == 1
    assert NodeType.DECISION in diagnostics["node_count_by_type"]

    stats = graph.statistics()
    assert stats["total_nodes"] == sum(diagnostics["node_count_by_type"].values())
    assert stats["total_nodes"] > 0
    assert "generated_at" in stats
    await bus.shutdown()


def test_watchlist_seeded_from_settings_at_construction():
    class _Portfolio:
        watchlist = ["NVDA", "AAPL"]

    class _Settings:
        portfolio = _Portfolio()

    graph = KnowledgeGraph(_Settings())
    watchlist_node = graph.node(f"{NodeType.WATCHLIST}:default")
    assert watchlist_node is not None
    tracked = graph.neighbors(watchlist_node.node_id, relation="tracks")
    assert {n.label for n in tracked} == {"NVDA", "AAPL"}


# ---------------------------------------------------------------- KnowledgeGraphQueryEngine


async def _graph_with_resolved_decisions(settings, *, wins: int, losses: int, strategy="Momentum Breakout", context_label="High Volatility") -> KnowledgeGraph:
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    for i in range(wins):
        await bus.publish(_decision(
            bar_index=i, outcome="correct", outcome_pending=False,
            strategy_matches=[strategy], market_context={"volatility": context_label},
        ))
    for i in range(losses):
        await bus.publish(_decision(
            bar_index=wins + i, outcome="incorrect", outcome_pending=False,
            strategy_matches=[strategy], market_context={"volatility": context_label},
        ))
    await bus.drain()
    await bus.shutdown()
    return graph


async def test_best_strategy_for_context_traces_win_rate_explainably(settings):
    graph = await _graph_with_resolved_decisions(settings, wins=4, losses=1)
    engine = KnowledgeGraphQueryEngine(graph, min_sample=2)

    result = engine.best_strategy_for_context("High Volatility")
    assert "Momentum Breakout" in result.answer
    assert result.explanation  # every claim traces back to concrete counts
    assert result.supporting_data.get("win_rate") == 0.8


async def test_best_strategy_for_context_reports_insufficient_data_honestly(settings):
    graph = await _graph_with_resolved_decisions(settings, wins=1, losses=0)
    engine = KnowledgeGraphQueryEngine(graph, min_sample=5)
    result = engine.best_strategy_for_context("High Volatility")
    assert "enough" in result.answer.lower() or "not" in result.answer.lower()


async def test_confidence_vs_actual_outcome_falls_back_to_graph_only_approximation(settings):
    graph = await _graph_with_resolved_decisions(settings, wins=3, losses=1)
    engine = KnowledgeGraphQueryEngine(graph)  # no calibration collaborator injected
    result = engine.confidence_vs_actual_outcome()
    assert result.answer
    assert result.explanation


async def test_confidence_vs_actual_outcome_delegates_when_calibration_wired(settings):
    graph = await _graph_with_resolved_decisions(settings, wins=2, losses=1)

    class _Bucket:
        def __init__(self, label, expected_rate, actual_win_rate, sample_size, verdict):
            self.label, self.expected_rate, self.actual_win_rate = label, expected_rate, actual_win_rate
            self.sample_size, self.verdict = sample_size, verdict

    class _Report:
        overall_verdict = "roughly well-calibrated"
        buckets = [_Bucket("70-80%", 0.75, 0.66, 3, "slightly overconfident")]

    class _Calibration:
        def report(self):
            return _Report()

    engine = KnowledgeGraphQueryEngine(graph, confidence_calibration=_Calibration())
    result = engine.confidence_vs_actual_outcome()
    assert result.answer == "roughly well-calibrated"
    assert any("Delegated to the Confidence Calibration service" in line for line in result.explanation)
    assert any("70-80%" in line for line in result.explanation)


# --------------------------------------------------------- best/strongest/weakest evidence combos


async def _graph_with_evidence_combos(settings) -> KnowledgeGraph:
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    # Combo A ("EMA: Bullish EMA Cross" + "RSI: Oversold Bounce") -- lower
    # confidence but a perfect win rate, repeated 3x.
    for i in range(3):
        await bus.publish(_decision(
            bar_index=i, confidence=80.0, outcome="correct", outcome_pending=False,
            technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)", "RSI: Oversold Bounce (bullish, 70/100)"],
        ))
    # Combo B ("ADX: Strong Trend" + "MACD: Bull Cross") -- higher average
    # confidence but every occurrence resolved incorrect, repeated 2x.
    for i in range(2):
        await bus.publish(_decision(
            bar_index=10 + i, confidence=90.0, outcome="incorrect", outcome_pending=False,
            technical_evidence=["ADX: Strong Trend (bullish, 90/100)", "MACD: Bull Cross (bullish, 90/100)"],
        ))
    await bus.drain()
    await bus.shutdown()
    return graph


async def test_best_evidence_combinations_ranks_by_average_confidence(settings):
    graph = await _graph_with_evidence_combos(settings)
    engine = KnowledgeGraphQueryEngine(graph, min_sample=2)
    result = engine.best_evidence_combinations()
    # Combo B has the higher average confidence (90 > 80), even though its
    # win rate is worse -- this method ranks by confidence, not win rate.
    assert "ADX: Strong Trend" in result.answer
    assert "MACD: Bull Cross" in result.answer
    assert result.supporting_data["average_confidence"] == 90.0


async def test_strongest_evidence_combinations_ranks_by_win_rate(settings):
    graph = await _graph_with_evidence_combos(settings)
    engine = KnowledgeGraphQueryEngine(graph, min_sample=2)
    result = engine.strongest_evidence_combinations()
    assert "EMA: Bullish EMA Cross" in result.answer
    assert result.supporting_data["win_rate"] == 1.0


async def test_weakest_evidence_combinations_ranks_by_win_rate(settings):
    graph = await _graph_with_evidence_combos(settings)
    engine = KnowledgeGraphQueryEngine(graph, min_sample=2)
    result = engine.weakest_evidence_combinations()
    assert "ADX: Strong Trend" in result.answer
    assert result.supporting_data["win_rate"] == 0.0


async def test_evidence_combination_queries_report_honestly_with_no_data(settings):
    graph = KnowledgeGraph(settings)
    engine = KnowledgeGraphQueryEngine(graph)
    assert "No decisions" in engine.best_evidence_combinations().answer
    assert "Not enough" in engine.strongest_evidence_combinations().answer
    assert "Not enough" in engine.weakest_evidence_combinations().answer


# --------------------------------------------------------- market regimes


async def test_worst_and_best_market_regimes_rank_by_win_rate(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    # "volatility" regime: 4 correct, 1 incorrect -> 80% win rate.
    for i in range(4):
        await bus.publish(_decision(bar_index=i, outcome="correct", outcome_pending=False, market_context={"volatility": "High Volatility"}))
    await bus.publish(_decision(bar_index=4, outcome="incorrect", outcome_pending=False, market_context={"volatility": "High Volatility"}))
    # "trend" regime: 1 correct, 3 incorrect -> 25% win rate.
    await bus.publish(_decision(bar_index=5, outcome="correct", outcome_pending=False, market_context={"trend": "Bull Trend"}))
    for i in range(3):
        await bus.publish(_decision(bar_index=6 + i, outcome="incorrect", outcome_pending=False, market_context={"trend": "Bull Trend"}))
    await bus.drain()

    engine = KnowledgeGraphQueryEngine(graph, min_sample=2)
    worst = engine.worst_market_regimes()
    assert worst.supporting_data["regime"] == "trend"
    best = engine.best_market_regimes()
    assert best.supporting_data["regime"] == "volatility"
    await bus.shutdown()


async def test_market_regime_queries_report_honestly_with_no_data(settings):
    graph = KnowledgeGraph(settings)
    engine = KnowledgeGraphQueryEngine(graph)
    assert "Not enough" in engine.worst_market_regimes().answer
    assert "Not enough" in engine.best_market_regimes().answer


# --------------------------------------------------------- recurring mistakes/strengths


async def test_recurring_mistakes_before_losing_streaks_finds_the_preceding_evidence(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # d0 (the "prior"): not itself part of the streak.
    await bus.publish(_decision(
        bar_index=0, timestamp=base, outcome="correct", outcome_pending=False,
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
    ))
    # d1, d2: two consecutive incorrect decisions -> a streak of length 2,
    # recognized exactly once, attributing d0's evidence.
    await bus.publish(_decision(bar_index=1, timestamp=base + timedelta(minutes=1), outcome="incorrect", outcome_pending=False))
    await bus.publish(_decision(bar_index=2, timestamp=base + timedelta(minutes=2), outcome="incorrect", outcome_pending=False))
    await bus.drain()

    engine = KnowledgeGraphQueryEngine(graph)
    result = engine.recurring_mistakes_before_losing_streaks(streak_length=2)
    assert "EMA: Bullish EMA Cross" in result.answer
    assert result.supporting_data["streaks_found"] == 1
    await bus.shutdown()


async def test_recurring_strengths_before_winning_streaks_finds_the_preceding_evidence(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    await bus.publish(_decision(
        bar_index=0, timestamp=base, outcome="incorrect", outcome_pending=False,
        technical_evidence=["RSI: Oversold Bounce (bullish, 70/100)"],
    ))
    await bus.publish(_decision(bar_index=1, timestamp=base + timedelta(minutes=1), outcome="correct", outcome_pending=False))
    await bus.publish(_decision(bar_index=2, timestamp=base + timedelta(minutes=2), outcome="correct", outcome_pending=False))
    await bus.drain()

    engine = KnowledgeGraphQueryEngine(graph)
    result = engine.recurring_strengths_before_winning_streaks(streak_length=2)
    assert "RSI: Oversold Bounce" in result.answer
    assert result.supporting_data["streaks_found"] == 1
    await bus.shutdown()


async def test_streak_queries_report_honestly_with_no_streaks_yet(settings):
    graph = await _graph_with_resolved_decisions(settings, wins=1, losses=1)
    engine = KnowledgeGraphQueryEngine(graph)
    assert "No losing streaks" in engine.recurring_mistakes_before_losing_streaks(streak_length=5).answer
    assert "No winning streaks" in engine.recurring_strengths_before_winning_streaks(streak_length=5).answer


# --------------------------------------------------------- macro-event delegation


async def test_best_strategies_during_market_event_delegates_to_best_strategy_for_context(settings):
    graph = await _graph_with_resolved_decisions(settings, wins=4, losses=1, context_label="Fed Week")
    engine = KnowledgeGraphQueryEngine(graph, min_sample=2)
    result = engine.best_strategies_during_market_event("Fed Week")
    assert "Fed Week" in result.question
    assert "Momentum Breakout" in result.answer


# --------------------------------------------------------- disagreeing indicators


async def test_indicators_that_disagree_most_counts_direction_mismatches(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    # EMA and RSI disagree twice (bullish vs. bearish)...
    for i in range(2):
        await bus.publish(_decision(bar_index=i, technical_evidence=[
            "EMA: Bullish EMA Cross (bullish, 80/100)", "RSI: Overbought (bearish, 70/100)",
        ]))
    # ...and agree once (both bullish).
    await bus.publish(_decision(bar_index=2, technical_evidence=[
        "EMA: Bullish EMA Cross (bullish, 80/100)", "RSI: Oversold Bounce (bullish, 70/100)",
    ]))
    await bus.drain()

    engine = KnowledgeGraphQueryEngine(graph)
    result = engine.indicators_that_disagree_most()
    assert "EMA" in result.answer and "RSI" in result.answer
    assert result.supporting_data["disagreements"] == 2
    assert result.supporting_data["co_occurrences"] == 3
    await bus.shutdown()


async def test_indicators_that_disagree_most_reports_honestly_with_no_data(settings):
    graph = KnowledgeGraph(settings)
    engine = KnowledgeGraphQueryEngine(graph)
    assert "Not enough decisions" in engine.indicators_that_disagree_most().answer


async def test_indicators_that_disagree_most_reports_honestly_when_nothing_ever_disagreed(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)
    await bus.publish(_decision(technical_evidence=[
        "EMA: Bullish EMA Cross (bullish, 80/100)", "RSI: Oversold Bounce (bullish, 70/100)",
    ]))
    await bus.drain()
    engine = KnowledgeGraphQueryEngine(graph)
    result = engine.indicators_that_disagree_most()
    assert "No two evidence sources have disagreed" in result.answer
    await bus.shutdown()


# --------------------------------------------------------- most reliable evidence sources


async def test_most_reliable_evidence_sources_delegates_to_reliability_engine(settings):
    graph = KnowledgeGraph(settings)

    class _Stat:
        def __init__(self, source, correct, total):
            self.source, self.correct, self.total = source, correct, total
            self.reliability = correct / total

    class _Reliability:
        def ranked(self, *, top_n):
            return [_Stat("EMA", 9, 10)][:top_n]

    engine = KnowledgeGraphQueryEngine(graph, evidence_reliability=_Reliability())
    result = engine.most_reliable_evidence_sources(top_n=1)
    assert "'EMA'" in result.answer
    assert any("Delegated to the Evidence Reliability Engine" in line for line in result.explanation)


async def test_most_reliable_evidence_sources_falls_back_to_graph_only_approximation(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)
    await bus.publish(_decision(
        outcome="correct", outcome_pending=False, simulated_action="watch_bullish",
        technical_evidence=["EMA: Bullish EMA Cross (bullish, 80/100)"],
    ))
    await bus.drain()

    engine = KnowledgeGraphQueryEngine(graph)  # no reliability collaborator injected
    result = engine.most_reliable_evidence_sources()
    assert "EMA" in result.answer
    assert any("Graph-only approximation" in line for line in result.explanation)
    await bus.shutdown()


async def test_most_reliable_evidence_sources_reports_honestly_with_no_data(settings):
    graph = KnowledgeGraph(settings)
    engine = KnowledgeGraphQueryEngine(graph)
    assert "No resolved decisions" in engine.most_reliable_evidence_sources().answer


# --------------------------------------------------------- false positives by context


async def test_market_contexts_generating_false_positives(settings):
    graph = KnowledgeGraph(settings)
    bus = EventBus.from_settings(settings)
    graph.attach(bus)

    # "Choppy" context: 2 of 2 matched decisions were incorrect -> 100%.
    for i in range(2):
        await bus.publish(_decision(bar_index=i, outcome="incorrect", outcome_pending=False, market_context={"volatility": "Choppy"}))
    # "High Volatility" context: 1 of 4 matched decisions was incorrect -> 25%.
    await bus.publish(_decision(bar_index=10, outcome="incorrect", outcome_pending=False, market_context={"volatility": "High Volatility"}))
    for i in range(3):
        await bus.publish(_decision(bar_index=11 + i, outcome="correct", outcome_pending=False, market_context={"volatility": "High Volatility"}))
    await bus.drain()

    engine = KnowledgeGraphQueryEngine(graph)
    result = engine.market_contexts_generating_false_positives()
    assert result.supporting_data["context"] == "Choppy"
    assert result.supporting_data["false_positive_rate"] == 1.0
    await bus.shutdown()


async def test_market_contexts_generating_false_positives_reports_honestly_with_no_data(settings):
    graph = KnowledgeGraph(settings)
    engine = KnowledgeGraphQueryEngine(graph)
    assert "Not enough" in engine.market_contexts_generating_false_positives().answer
