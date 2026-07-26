"""
The Trading Knowledge Graph.

Per PROJECT.md's Milestone 12 spec: "Instead of isolated records, the
platform should understand relationships." Not a plugin — a core service,
the same tier as the Decision Timeline or Capital Protection Engine.
Builds a deterministic, explainable graph purely by observing events
already flowing over the bus (``DecisionRecorded``, ``ReflectionGenerated``,
``JournalCreated``, ``RiskEvent``, ``StrategyMatched``,
``MarketContextUpdated``, ``CoachingEvent``) — never by reaching into
another engine's internals, and never any machine learning: every edge is
added by an explicit, readable rule, so every answer the Query Layer
(``app/knowledge_graph/query.py``) gives can be explained by naming the
exact nodes and edges it traced.

Every node type from the spec is modeled (see
:mod:`app.knowledge_graph.models`). ``Decision`` is deliberately the hub
most other node types connect through — a Strategy, a piece of Evidence, a
Market Context, and a Risk Profile are each linked to the Decision they
were involved in, rather than directly to each other — so the spec's
example chain

    Strategy -> Evidence Pattern -> Market Context -> Risk Profile -> Outcome

is answerable as a short walk through each Decision node that connects
them, without a combinatorial explosion of direct edges between every
pair of node types.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

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
from app.evidence.formatting import parse_evidence_line
from app.knowledge_graph.models import Edge, Node, NodeType
from app.logging import get_logger

log = get_logger(__name__)

#: Bounded so a long-running deployment (or a long simulation) never grows
#: this engine's in-memory footprint without limit -- the durable,
#: unbounded history always remains queryable from the database via
#: EventLogRepository. Edges are far more numerous than nodes (many
#: decisions reuse the same strategy/evidence/context nodes), so this
#: bounds edges directly rather than nodes.
_DEFAULT_MAX_EDGES = 20_000


class KnowledgeGraph:
    """Maintains a bounded, queryable, in-memory graph of trading-relevant
    entities and their relationships. Attach once at bootstrap (or once
    per Simulation Engine run — see ``app/simulation/engine.py``); every
    consumer (the Query Layer, the Learning Engine, ``/coach``) reads it
    via ``node()``/``nodes_by_type()``/``neighbors()``/``edges_from()``/
    ``edges_to()`` — the same read-only-query pattern every other core
    engine in this codebase exposes. Never mutated by anything except its
    own event handlers."""

    def __init__(self, settings: Any, *, max_edges: int | None = None) -> None:
        self._max_edges = max_edges if max_edges is not None else _DEFAULT_MAX_EDGES
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._edges_from: dict[str, list[Edge]] = defaultdict(list)
        self._edges_to: dict[str, list[Edge]] = defaultdict(list)
        #: Cached from the most recently observed RiskEvent per symbol (or
        #: "" for portfolio-wide) -- the same cache-only pattern the
        #: Reflection Engine already established for confidence_trend
        #: (app/reflection/engine.py) -- never a live call into the
        #: Capital Protection Engine.
        self._active_profile_by_symbol: dict[str, str] = {}
        self._event_bus: EventBus | None = None
        self._total_events_observed = 0

        # Seed watchlist -> symbol edges from configured settings, a
        # one-time snapshot at construction time (watchlist membership
        # rarely changes at runtime; a symbol added later is still picked
        # up the first time it appears in any observed event).
        watchlist = list(getattr(getattr(settings, "portfolio", None), "watchlist", None) or [])
        if watchlist:
            watchlist_node = self._upsert_node(NodeType.WATCHLIST, "default", "default watchlist")
            for symbol in watchlist:
                symbol_node = self._upsert_node(NodeType.SYMBOL, symbol, symbol)
                self._add_edge(watchlist_node.node_id, symbol_node.node_id, "tracks")

    def attach(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(DecisionRecorded, self._on_decision_recorded, name="knowledge_graph_decisions")
        event_bus.subscribe(ReflectionGenerated, self._on_reflection_generated, name="knowledge_graph_reflections")
        event_bus.subscribe(JournalCreated, self._on_journal_created, name="knowledge_graph_journal")
        event_bus.subscribe(RiskEvent, self._on_risk_event, name="knowledge_graph_risk")
        event_bus.subscribe(StrategyMatched, self._on_strategy_matched, name="knowledge_graph_strategy")
        event_bus.subscribe(MarketContextUpdated, self._on_market_context_updated, name="knowledge_graph_context")
        event_bus.subscribe(CoachingEvent, self._on_coaching_event, name="knowledge_graph_coaching")
        log.info("knowledge_graph_attached", max_edges=self._max_edges)

    # ---------------------------------------------------------------- node/edge upsert

    def _upsert_node(self, node_type: str, key: str, label: str, **attributes: Any) -> Node:
        node_id = f"{node_type}:{key}"
        existing = self._nodes.get(node_id)
        if existing is not None:
            if attributes:
                existing.attributes.update(attributes)
            return existing
        node = Node(node_id=node_id, node_type=node_type, label=label, attributes=dict(attributes))
        self._nodes[node_id] = node
        return node

    def _add_edge(self, source_id: str, target_id: str, relation: str, **attributes: Any) -> None:
        # Dedupe identical (source, target, relation) triples -- repeated
        # observations (e.g. the same strategy matching the same symbol
        # again) refresh attributes rather than piling up duplicate edges.
        for edge in self._edges_from[source_id]:
            if edge.target_id == target_id and edge.relation == relation:
                edge.attributes.update(attributes)
                return
        edge = Edge(source_id=source_id, target_id=target_id, relation=relation, attributes=dict(attributes))
        self._edges.append(edge)
        self._edges_from[source_id].append(edge)
        self._edges_to[target_id].append(edge)
        if len(self._edges) > self._max_edges:
            oldest = self._edges.pop(0)
            self._edges_from[oldest.source_id].remove(oldest)
            self._edges_to[oldest.target_id].remove(oldest)

    # ---------------------------------------------------------------- event handlers

    async def _on_decision_recorded(self, event: DecisionRecorded) -> None:
        self._total_events_observed += 1
        decision = self._upsert_node(
            NodeType.DECISION,
            str(event.event_id),
            f"{event.symbol} decision @ bar {event.bar_index}",
            symbol=event.symbol,
            simulated_action=event.simulated_action,
            confidence=event.confidence,
            outcome=event.outcome,
            outcome_pending=event.outcome_pending,
            timestamp=event.timestamp.isoformat(),
        )
        symbol_node = self._upsert_node(NodeType.SYMBOL, event.symbol, event.symbol)
        self._add_edge(symbol_node.node_id, decision.node_id, "has_decision")

        if event.correlation_id is not None:
            simulation_node = self._upsert_node(
                NodeType.SIMULATION, str(event.correlation_id), f"simulation run {event.correlation_id}"
            )
            self._add_edge(simulation_node.node_id, decision.node_id, "produced_decision")

        for strategy_name in event.strategy_matches:
            strategy_node = self._upsert_node(NodeType.STRATEGY, strategy_name, strategy_name)
            self._add_edge(decision.node_id, strategy_node.node_id, "matched_strategy")

        for line in event.technical_evidence:
            self._link_evidence(decision, line, NodeType.INDICATOR)
        for line in event.fundamental_evidence:
            self._link_evidence(decision, line, NodeType.EXTERNAL_INTELLIGENCE)

        for context_type, label in event.market_context.items():
            context_node = self._upsert_node(NodeType.MARKET_CONTEXT, f"{context_type}:{label}", label, context_type=context_type)
            regime_node = self._upsert_node(NodeType.MARKET_REGIME, context_type, context_type)
            self._add_edge(context_node.node_id, regime_node.node_id, "belongs_to_regime")
            self._add_edge(decision.node_id, context_node.node_id, "occurred_during")

        active_profile = self._active_profile_by_symbol.get(event.symbol) or self._active_profile_by_symbol.get("")
        if active_profile:
            profile_node = self._upsert_node(NodeType.RISK_PROFILE, active_profile, active_profile)
            self._add_edge(decision.node_id, profile_node.node_id, "evaluated_under")

        if not event.outcome_pending and event.outcome is not None:
            outcome_node = self._upsert_node(
                NodeType.OUTCOME,
                str(event.event_id),
                event.outcome,
                outcome=event.outcome,
                outcome_price_change_pct=event.outcome_price_change_pct,
            )
            self._add_edge(decision.node_id, outcome_node.node_id, "resulted_in")

    def _link_evidence(self, decision: Node, line: str, source_node_type: str) -> None:
        parsed = parse_evidence_line(line)
        if parsed is None:
            return
        evidence_node = self._upsert_node(
            NodeType.EVIDENCE, f"{parsed.source}:{parsed.title}", f"{parsed.source}: {parsed.title}"
        )
        source_node = self._upsert_node(source_node_type, parsed.source, parsed.source)
        self._add_edge(evidence_node.node_id, source_node.node_id, "produced_by")
        self._add_edge(decision.node_id, evidence_node.node_id, "considered_evidence", direction=parsed.direction, confidence=parsed.confidence)

    async def _on_reflection_generated(self, event: ReflectionGenerated) -> None:
        self._total_events_observed += 1
        reflection_node = self._upsert_node(
            NodeType.REFLECTION,
            str(event.event_id),
            event.lessons_learned or f"reflection on {event.symbol}",
            confidence_evolution=event.confidence_evolution,
        )
        decision_id = f"{NodeType.DECISION}:{event.decision_event_id}"
        self._add_edge(decision_id, reflection_node.node_id, "reflected_by")

    async def _on_journal_created(self, event: JournalCreated) -> None:
        self._total_events_observed += 1
        if not event.note:
            return
        journal_node = self._upsert_node(
            NodeType.JOURNAL_ENTRY, str(event.event_id), event.note[:80], author=event.author
        )
        if event.decision_event_id is not None:
            decision_id = f"{NodeType.DECISION}:{event.decision_event_id}"
            self._add_edge(decision_id, journal_node.node_id, "annotated_by")
        elif event.symbol is not None:
            symbol_node = self._upsert_node(NodeType.SYMBOL, event.symbol, event.symbol)
            self._add_edge(symbol_node.node_id, journal_node.node_id, "annotated_by")

    async def _on_risk_event(self, event: RiskEvent) -> None:
        self._total_events_observed += 1
        if not event.profile_name:
            return
        self._active_profile_by_symbol[event.symbol or ""] = event.profile_name
        profile_node = self._upsert_node(NodeType.RISK_PROFILE, event.profile_name, event.profile_name)
        if event.symbol is not None:
            symbol_node = self._upsert_node(NodeType.SYMBOL, event.symbol, event.symbol)
            self._add_edge(symbol_node.node_id, profile_node.node_id, "evaluated_under")

    async def _on_strategy_matched(self, event: StrategyMatched) -> None:
        self._total_events_observed += 1
        strategy_node = self._upsert_node(NodeType.STRATEGY, event.strategy, event.strategy)
        symbol_node = self._upsert_node(NodeType.SYMBOL, event.symbol, event.symbol)
        self._add_edge(symbol_node.node_id, strategy_node.node_id, "matched", score=event.score)

    async def _on_market_context_updated(self, event: MarketContextUpdated) -> None:
        self._total_events_observed += 1
        context_node = self._upsert_node(
            NodeType.MARKET_CONTEXT, f"{event.context_type}:{event.label}", event.label, context_type=event.context_type
        )
        regime_node = self._upsert_node(NodeType.MARKET_REGIME, event.context_type, event.context_type)
        self._add_edge(context_node.node_id, regime_node.node_id, "belongs_to_regime")
        if event.symbol is not None:
            symbol_node = self._upsert_node(NodeType.SYMBOL, event.symbol, event.symbol)
            self._add_edge(symbol_node.node_id, context_node.node_id, "in_context")

    async def _on_coaching_event(self, event: CoachingEvent) -> None:
        self._total_events_observed += 1
        coaching_node = self._upsert_node(
            "coaching", str(event.event_id), event.title, pattern_type=event.pattern_type, priority=event.priority
        )
        if event.symbol is not None:
            symbol_node = self._upsert_node(NodeType.SYMBOL, event.symbol, event.symbol)
            self._add_edge(symbol_node.node_id, coaching_node.node_id, "coached_about")
        for strategy_name in event.related_strategies:
            strategy_node = self._upsert_node(NodeType.STRATEGY, strategy_name, strategy_name)
            self._add_edge(coaching_node.node_id, strategy_node.node_id, "relates_to_strategy")

    # ---------------------------------------------------------------- queries

    def node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def nodes_by_type(self, node_type: str) -> list[Node]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    def edges_from(self, node_id: str, *, relation: str | None = None) -> list[Edge]:
        edges = self._edges_from.get(node_id, [])
        if relation is not None:
            edges = [e for e in edges if e.relation == relation]
        return list(edges)

    def edges_to(self, node_id: str, *, relation: str | None = None) -> list[Edge]:
        edges = self._edges_to.get(node_id, [])
        if relation is not None:
            edges = [e for e in edges if e.relation == relation]
        return list(edges)

    def neighbors(self, node_id: str, *, relation: str | None = None, direction: str = "out") -> list[Node]:
        """``direction="out"`` (default) follows edges away from
        ``node_id``; ``"in"`` follows edges pointing at it."""
        edges = self.edges_from(node_id, relation=relation) if direction == "out" else self.edges_to(node_id, relation=relation)
        ids = (e.target_id for e in edges) if direction == "out" else (e.source_id for e in edges)
        return [n for n in (self._nodes.get(i) for i in ids) if n is not None]

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "nodes": len(self._nodes), "edges": len(self._edges)}

    def diagnostics(self) -> dict[str, Any]:
        by_type: dict[str, int] = defaultdict(int)
        for n in self._nodes.values():
            by_type[n.node_type] += 1
        return {
            "total_events_observed": self._total_events_observed,
            "node_count_by_type": dict(sorted(by_type.items())),
            "max_edges": self._max_edges,
            "at_capacity": len(self._edges) >= self._max_edges,
        }

    def statistics(self) -> dict[str, Any]:
        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "total_events_observed": self._total_events_observed,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
