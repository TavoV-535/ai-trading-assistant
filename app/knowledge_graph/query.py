"""
The Explainable Query Layer over the Trading Knowledge Graph.

Per PROJECT.md's Milestone 12 spec: "The answers must be explainable by
tracing graph relationships." Every method here returns a
:class:`QueryResult` whose ``explanation`` is a literal, ordered trace of
the nodes/edges/aggregation steps that produced ``answer`` — never an
opaque number. No machine learning anywhere in this module: every query
is a deterministic graph traversal plus arithmetic (counts, rates,
averages) over what :class:`~app.knowledge_graph.graph.KnowledgeGraph`
already observed.

This is also the "Query Layer" from the Milestone 12 architectural
recommendations — "future AI providers, dashboards, APIs, and reports all
consume the same explainable query interface" — so every method is a
plain, synchronous, read-only function of already-built state, safe to
call from a Discord command, a future HTTP endpoint, or a future AI
provider's tool call alike.

Where a query would otherwise duplicate a calculation another Milestone
12 service already owns (evidence reliability, confidence calibration),
this layer *delegates* to that service (optional constructor injection)
rather than recomputing it — "no duplicated calculations." Each such
method still works standalone (a documented, graph-only approximation)
if that collaborator isn't wired, so the Query Layer is never a hard
dependency on the rest of Milestone 12.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field

from app.knowledge_graph.graph import KnowledgeGraph
from app.knowledge_graph.models import Node, NodeType

_MIN_SAMPLE_DEFAULT = 2


class QueryResult(BaseModel):
    """One answered question. ``explanation`` is the whole point of this
    layer — an ordered list of plain-English steps ("considered 14
    decisions matching 'Momentum Breakout'", "9 of 14 resolved correct",
    ...) that a person (or a future AI provider) can verify against the
    graph themselves."""

    question: str
    answer: str
    explanation: list[str] = Field(default_factory=list)
    supporting_data: dict[str, Any] = Field(default_factory=dict)


def _outcome_label(graph: KnowledgeGraph, decision: Node) -> str | None:
    outcomes = graph.neighbors(decision.node_id, relation="resulted_in")
    return outcomes[0].label if outcomes else None


def _win_rate(graph: KnowledgeGraph, decisions: list[Node]) -> tuple[float, int, int]:
    """Returns ``(win_rate, wins, resolved_count)`` over ``decisions`` —
    ``resolved_count`` excludes decisions still ``outcome_pending``."""
    resolved = [d for d in decisions if not d.attributes.get("outcome_pending", True)]
    wins = sum(1 for d in resolved if d.attributes.get("outcome") == "correct")
    rate = (wins / len(resolved)) if resolved else 0.0
    return rate, wins, len(resolved)


class KnowledgeGraphQueryEngine:
    """Wraps a :class:`~app.knowledge_graph.graph.KnowledgeGraph` with the
    Milestone 12 spec's example explainable questions. Attach once
    (constructed from a live graph, no separate ``attach()`` needed — it
    never subscribes to the bus itself, only reads the graph on demand)."""

    def __init__(
        self,
        graph: KnowledgeGraph,
        *,
        evidence_reliability: Any = None,
        confidence_calibration: Any = None,
        min_sample: int = _MIN_SAMPLE_DEFAULT,
    ) -> None:
        self._graph = graph
        self._evidence_reliability = evidence_reliability
        self._confidence_calibration = confidence_calibration
        self._min_sample = min_sample

    # ---------------------------------------------------------------- 1

    def best_strategy_for_context(self, context_label: str | None = None) -> QueryResult:
        """"Which strategy performs best during high volatility?" (or any
        other context label)."""
        question = f"Which strategy performs best during {context_label}?" if context_label else "Which strategy performs best overall?"
        context_nodes = self._matching_context_nodes(context_label) if context_label else self._graph.nodes_by_type(NodeType.MARKET_CONTEXT)
        if not context_nodes:
            return QueryResult(question=question, answer="No matching market context has been observed yet.", explanation=["No MarketContext nodes matched."])

        decision_ids_in_context: set[str] = set()
        for ctx in context_nodes:
            for edge in self._graph.edges_to(ctx.node_id, relation="occurred_during"):
                decision_ids_in_context.add(edge.source_id)

        best: tuple[str, float, int, int] | None = None
        explanation = [f"Considered {len(context_nodes)} matching market context node(s): {[c.label for c in context_nodes]}."]
        for strategy in self._graph.nodes_by_type(NodeType.STRATEGY):
            strategy_decisions = [
                self._graph.node(e.source_id)
                for e in self._graph.edges_to(strategy.node_id, relation="matched_strategy")
                if e.source_id in decision_ids_in_context
            ]
            strategy_decisions = [d for d in strategy_decisions if d is not None]
            rate, wins, resolved = _win_rate(self._graph, strategy_decisions)
            if resolved < self._min_sample:
                continue
            explanation.append(f"'{strategy.label}': {wins}/{resolved} resolved decisions correct ({rate:.0%}).")
            if best is None or rate > best[1]:
                best = (strategy.label, rate, wins, resolved)

        if best is None:
            return QueryResult(
                question=question,
                answer=f"Not enough resolved decisions yet (need at least {self._min_sample} per strategy).",
                explanation=explanation,
            )
        name, rate, wins, resolved = best
        return QueryResult(
            question=question,
            answer=f"'{name}' — {wins}/{resolved} correct ({rate:.0%}) among decisions in this context.",
            explanation=explanation,
            supporting_data={"strategy": name, "win_rate": rate, "sample_size": resolved},
        )

    def _matching_context_nodes(self, label: str) -> list[Node]:
        needle = label.lower()
        return [n for n in self._graph.nodes_by_type(NodeType.MARKET_CONTEXT) if needle in n.label.lower()]

    # ---------------------------------------------------------------- 2

    def best_evidence_combinations(self) -> QueryResult:
        """"What evidence combinations produce the highest confidence?" —
        ranked by average confidence *and* win rate among decisions that
        considered the same pair of evidence sources together."""
        question = "What evidence combinations produce the highest confidence?"
        combo_decisions: dict[tuple[str, str], list[Node]] = defaultdict(list)
        combo_confidence: dict[tuple[str, str], list[float]] = defaultdict(list)

        for decision in self._graph.nodes_by_type(NodeType.DECISION):
            evidence_edges = self._graph.edges_from(decision.node_id, relation="considered_evidence")
            sources = sorted({self._graph.node(e.target_id).label for e in evidence_edges if self._graph.node(e.target_id)})
            for pair in combinations(sources, 2):
                combo_decisions[pair].append(decision)
                combo_confidence[pair].append(decision.attributes.get("confidence", 0.0))

        if not combo_decisions:
            return QueryResult(question=question, answer="No decisions with 2+ pieces of evidence observed yet.", explanation=[])

        ranked = []
        for pair, decisions in combo_decisions.items():
            if len(decisions) < self._min_sample:
                continue
            avg_conf = sum(combo_confidence[pair]) / len(combo_confidence[pair])
            rate, wins, resolved = _win_rate(self._graph, decisions)
            ranked.append((pair, avg_conf, rate, wins, resolved))

        if not ranked:
            return QueryResult(
                question=question,
                answer=f"Not enough repeated evidence combinations yet (need at least {self._min_sample} occurrences).",
                explanation=[],
            )
        ranked.sort(key=lambda r: r[1], reverse=True)
        pair, avg_conf, rate, wins, resolved = ranked[0]
        explanation = [f"'{a}' + '{b}' appeared together in {len(combo_decisions[pair])} decision(s), avg confidence {avg_conf:.1f}, {wins}/{resolved} resolved correct ({rate:.0%})." for a, b in [pair]]
        return QueryResult(
            question=question,
            answer=f"'{pair[0]}' + '{pair[1]}' — average confidence {avg_conf:.1f}/100, {wins}/{resolved} correct ({rate:.0%}).",
            explanation=explanation,
            supporting_data={"combination": list(pair), "average_confidence": avg_conf, "win_rate": rate},
        )

    def _evidence_combo_win_rates(self) -> list[tuple[tuple[str, str], float, int, int]]:
        """Every repeated 2-evidence-source combination's win rate,
        ascending -- the "performance" ranking (distinct from
        :meth:`best_evidence_combinations`'s confidence ranking). Shared by
        :meth:`strongest_evidence_combinations`/:meth:`weakest_evidence_combinations`."""
        combo_decisions: dict[tuple[str, str], list[Node]] = defaultdict(list)
        for decision in self._graph.nodes_by_type(NodeType.DECISION):
            evidence_edges = self._graph.edges_from(decision.node_id, relation="considered_evidence")
            sources = sorted({self._graph.node(e.target_id).label for e in evidence_edges if self._graph.node(e.target_id)})
            for pair in combinations(sources, 2):
                combo_decisions[pair].append(decision)
        ranked = []
        for pair, decisions in combo_decisions.items():
            if len(decisions) < self._min_sample:
                continue
            rate, wins, resolved = _win_rate(self._graph, decisions)
            ranked.append((pair, rate, wins, resolved))
        ranked.sort(key=lambda r: r[1])
        return ranked

    def strongest_evidence_combinations(self) -> QueryResult:
        """The Learning Engine's "strongest-performing evidence
        combinations" pattern — ranked by win rate, not confidence."""
        question = "Which evidence combinations perform best?"
        ranked = self._evidence_combo_win_rates()
        if not ranked:
            return QueryResult(question=question, answer="Not enough repeated evidence combinations yet.", explanation=[])
        pair, rate, wins, resolved = ranked[-1]
        return QueryResult(
            question=question,
            answer=f"'{pair[0]}' + '{pair[1]}' — {wins}/{resolved} correct ({rate:.0%}), the highest observed.",
            explanation=[f"'{r[0][0]}' + '{r[0][1]}': {r[2]}/{r[3]} correct ({r[1]:.0%})." for r in reversed(ranked)],
            supporting_data={"combination": list(pair), "win_rate": rate},
        )

    def weakest_evidence_combinations(self) -> QueryResult:
        """The mirror of :meth:`strongest_evidence_combinations` — ranked
        by lowest win rate."""
        question = "Which evidence combinations perform worst?"
        ranked = self._evidence_combo_win_rates()
        if not ranked:
            return QueryResult(question=question, answer="Not enough repeated evidence combinations yet.", explanation=[])
        pair, rate, wins, resolved = ranked[0]
        return QueryResult(
            question=question,
            answer=f"'{pair[0]}' + '{pair[1]}' — {wins}/{resolved} correct ({rate:.0%}), the lowest observed.",
            explanation=[f"'{r[0][0]}' + '{r[0][1]}': {r[2]}/{r[3]} correct ({r[1]:.0%})." for r in ranked],
            supporting_data={"combination": list(pair), "win_rate": rate},
        )

    # ---------------------------------------------------------------- 3

    def _regime_win_rates(self) -> list[tuple[str, float, int, int]]:
        """Every ``market_regime`` node's win rate (regimes group context
        labels by ``context_type`` -- trend, volatility, ...), ascending.
        Shared by :meth:`worst_market_regimes`/:meth:`best_market_regimes`
        so both trace the identical calculation, never a duplicate."""
        ranked = []
        for regime in self._graph.nodes_by_type(NodeType.MARKET_REGIME):
            context_nodes = self._graph.neighbors(regime.node_id, relation="belongs_to_regime", direction="in")
            decision_ids: set[str] = set()
            for ctx in context_nodes:
                for edge in self._graph.edges_to(ctx.node_id, relation="occurred_during"):
                    decision_ids.add(edge.source_id)
            decisions = [d for d in (self._graph.node(i) for i in decision_ids) if d is not None]
            rate, wins, resolved = _win_rate(self._graph, decisions)
            if resolved < self._min_sample:
                continue
            ranked.append((regime.label, rate, wins, resolved))
        ranked.sort(key=lambda r: r[1])
        return ranked

    def worst_market_regimes(self) -> QueryResult:
        """"What market regimes consistently reduce performance?" —
        ranked by lowest win rate."""
        question = "What market regimes consistently reduce performance?"
        ranked = self._regime_win_rates()
        if not ranked:
            return QueryResult(question=question, answer="Not enough resolved decisions across market regimes yet.", explanation=[])
        name, rate, wins, resolved = ranked[0]
        explanation = [f"'{r[0]}' regime: {r[2]}/{r[3]} correct ({r[1]:.0%})." for r in ranked]
        return QueryResult(
            question=question,
            answer=f"The '{name}' regime — {wins}/{resolved} correct ({rate:.0%}), the lowest observed.",
            explanation=explanation,
            supporting_data={"regime": name, "win_rate": rate},
        )

    def best_market_regimes(self) -> QueryResult:
        """The mirror of :meth:`worst_market_regimes` — ranked by highest
        win rate."""
        question = "What market regimes consistently improve performance?"
        ranked = self._regime_win_rates()
        if not ranked:
            return QueryResult(question=question, answer="Not enough resolved decisions across market regimes yet.", explanation=[])
        name, rate, wins, resolved = ranked[-1]
        explanation = [f"'{r[0]}' regime: {r[2]}/{r[3]} correct ({r[1]:.0%})." for r in reversed(ranked)]
        return QueryResult(
            question=question,
            answer=f"The '{name}' regime — {wins}/{resolved} correct ({rate:.0%}), the highest observed.",
            explanation=explanation,
            supporting_data={"regime": name, "win_rate": rate},
        )

    # ---------------------------------------------------------------- 4

    def _evidence_before_streaks(self, *, target_outcome: str, streak_length: int) -> tuple[int, dict[str, int]]:
        """Shared streak-detection walk: finds runs of ``streak_length``+
        consecutive decisions with ``outcome == target_outcome`` (per
        symbol, in decision order) and tallies which evidence source most
        often appeared in the decision immediately before each streak
        began. Shared by :meth:`recurring_mistakes_before_losing_streaks`
        (``target_outcome="incorrect"``) and
        :meth:`recurring_strengths_before_winning_streaks`
        (``target_outcome="correct"``) — one calculation, two verdicts."""
        preceding_sources: list[str] = []
        streaks_found = 0
        for symbol_node in self._graph.nodes_by_type(NodeType.SYMBOL):
            decisions = [self._graph.node(e.target_id) for e in self._graph.edges_from(symbol_node.node_id, relation="has_decision")]
            decisions = [d for d in decisions if d is not None]
            decisions.sort(key=lambda d: d.attributes.get("timestamp", ""))
            run = 0
            for i, d in enumerate(decisions):
                if d.attributes.get("outcome") == target_outcome:
                    run += 1
                    if run == streak_length:
                        streaks_found += 1
                        prior_index = i - streak_length
                        if prior_index >= 0:
                            prior = decisions[prior_index]
                            for e in self._graph.edges_from(prior.node_id, relation="considered_evidence"):
                                node = self._graph.node(e.target_id)
                                if node is not None:
                                    preceding_sources.append(node.label)
                else:
                    run = 0
        counts: dict[str, int] = defaultdict(int)
        for s in preceding_sources:
            counts[s] += 1
        return streaks_found, dict(counts)

    def recurring_mistakes_before_losing_streaks(self, *, streak_length: int = 2) -> QueryResult:
        """"What recurring mistakes precede losing streaks?" — finds runs
        of ``streak_length`` or more consecutive incorrect outcomes (per
        symbol, in decision order) and reports the evidence source most
        often present in the decision immediately before each streak."""
        question = "What recurring mistakes precede losing streaks?"
        streaks_found, counts = self._evidence_before_streaks(target_outcome="incorrect", streak_length=streak_length)
        if streaks_found == 0:
            return QueryResult(question=question, answer=f"No losing streaks of {streak_length}+ have occurred yet.", explanation=[])
        if not counts:
            return QueryResult(question=question, answer=f"Found {streaks_found} losing streak(s), but no common evidence pattern preceded them.", explanation=[])
        top = max(counts.items(), key=lambda kv: kv[1])
        explanation = [f"Found {streaks_found} losing streak(s) of {streak_length}+.", f"Evidence seen right before a streak: {counts}."]
        return QueryResult(
            question=question,
            answer=f"'{top[0]}' evidence appeared right before {top[1]} of {streaks_found} losing streak(s).",
            explanation=explanation,
            supporting_data={"streaks_found": streaks_found, "evidence_counts": counts},
        )

    def recurring_strengths_before_winning_streaks(self, *, streak_length: int = 2) -> QueryResult:
        """The mirror of :meth:`recurring_mistakes_before_losing_streaks` —
        what tends to precede a *winning* streak."""
        question = "What recurring strengths precede winning streaks?"
        streaks_found, counts = self._evidence_before_streaks(target_outcome="correct", streak_length=streak_length)
        if streaks_found == 0:
            return QueryResult(question=question, answer=f"No winning streaks of {streak_length}+ have occurred yet.", explanation=[])
        if not counts:
            return QueryResult(question=question, answer=f"Found {streaks_found} winning streak(s), but no common evidence pattern preceded them.", explanation=[])
        top = max(counts.items(), key=lambda kv: kv[1])
        explanation = [f"Found {streaks_found} winning streak(s) of {streak_length}+.", f"Evidence seen right before a streak: {counts}."]
        return QueryResult(
            question=question,
            answer=f"'{top[0]}' evidence appeared right before {top[1]} of {streaks_found} winning streak(s).",
            explanation=explanation,
            supporting_data={"streaks_found": streaks_found, "evidence_counts": counts},
        )

    # ---------------------------------------------------------------- 5

    def best_strategies_during_market_event(self, event_label: str = "Fed Week") -> QueryResult:
        """"What strategies perform best during Fed weeks?" (or any other
        macro-event context label). Delegates straight to
        :meth:`best_strategy_for_context`, which builds its own question
        text -- this method exists only to give the spec's exact example
        question its own named, documented entry point."""
        return self.best_strategy_for_context(event_label)

    # ---------------------------------------------------------------- 6

    def indicators_that_disagree_most(self) -> QueryResult:
        """"Which indicators disagree most often?" — counts, across
        decisions where both appeared, how often two evidence sources'
        recorded ``direction`` differed."""
        question = "Which indicators disagree most often?"
        disagreements: dict[tuple[str, str], int] = defaultdict(int)
        co_occurrences: dict[tuple[str, str], int] = defaultdict(int)

        for decision in self._graph.nodes_by_type(NodeType.DECISION):
            edges = self._graph.edges_from(decision.node_id, relation="considered_evidence")
            by_source: dict[str, str] = {}
            for e in edges:
                node = self._graph.node(e.target_id)
                if node is None:
                    continue
                source = node.label.split(":", 1)[0]
                by_source[source] = e.attributes.get("direction", "neutral")
            for a, b in combinations(sorted(by_source), 2):
                co_occurrences[(a, b)] += 1
                if by_source[a] != by_source[b]:
                    disagreements[(a, b)] += 1

        if not co_occurrences:
            return QueryResult(question=question, answer="Not enough decisions with multiple evidence sources yet.", explanation=[])

        ranked = sorted(disagreements.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked or ranked[0][1] == 0:
            return QueryResult(question=question, answer="No two evidence sources have disagreed yet.", explanation=[f"Co-occurrence counts: {dict(co_occurrences)}."])

        (a, b), count = ranked[0]
        total = co_occurrences[(a, b)]
        explanation = [f"'{a}' and '{b}' appeared together in {total} decision(s) and disagreed in {count} of them."]
        return QueryResult(
            question=question,
            answer=f"'{a}' and '{b}' — disagreed {count} of {total} times they appeared together ({count / total:.0%}).",
            explanation=explanation,
            supporting_data={"pair": [a, b], "disagreements": count, "co_occurrences": total},
        )

    # ---------------------------------------------------------------- 7

    def most_reliable_evidence_sources(self, *, top_n: int = 3) -> QueryResult:
        """"Which evidence sources are historically most reliable?" —
        delegates to the Evidence Reliability Engine when available (no
        duplicated calculation); falls back to a graph-only approximation
        (evidence's recorded direction vs. the decision's eventual
        outcome) otherwise."""
        question = "Which evidence sources are historically most reliable?"
        if self._evidence_reliability is not None:
            ranked = self._evidence_reliability.ranked(top_n=top_n)
            if not ranked:
                return QueryResult(question=question, answer="No evidence reliability statistics recorded yet.", explanation=["Delegated to the Evidence Reliability Engine."])
            explanation = ["Delegated to the Evidence Reliability Engine's tracked statistics (no duplicated calculation)."]
            explanation += [f"'{stat.source}': {stat.correct}/{stat.total} agreements with the eventual outcome ({stat.reliability:.0%})." for stat in ranked]
            top = ranked[0]
            return QueryResult(
                question=question,
                answer=f"'{top.source}' — {top.correct}/{top.total} agreements with the eventual outcome ({top.reliability:.0%}).",
                explanation=explanation,
                supporting_data={"ranked": [s.source for s in ranked]},
            )

        # Graph-only fallback.
        agree: dict[str, int] = defaultdict(int)
        total: dict[str, int] = defaultdict(int)
        for decision in self._graph.nodes_by_type(NodeType.DECISION):
            outcome = decision.attributes.get("outcome")
            if outcome not in ("correct", "incorrect"):
                continue
            for e in self._graph.edges_from(decision.node_id, relation="considered_evidence"):
                node = self._graph.node(e.target_id)
                if node is None:
                    continue
                source = node.label.split(":", 1)[0]
                total[source] += 1
                agreed = (e.attributes.get("direction") in ("bullish",) and outcome == "correct" and decision.attributes.get("simulated_action") == "watch_bullish") or (
                    e.attributes.get("direction") in ("bearish",) and outcome == "correct" and decision.attributes.get("simulated_action") == "watch_bearish"
                )
                if agreed:
                    agree[source] += 1
        if not total:
            return QueryResult(question=question, answer="No resolved decisions with evidence observed yet.", explanation=["Graph-only approximation (Evidence Reliability Engine not wired)."])
        ranked_sources = sorted(total, key=lambda s: (agree[s] / total[s]) if total[s] else 0.0, reverse=True)[:top_n]
        best = ranked_sources[0]
        explanation = ["Graph-only approximation (Evidence Reliability Engine not wired)."]
        explanation += [f"'{s}': {agree[s]}/{total[s]} agreements with a correct outcome ({(agree[s] / total[s]) if total[s] else 0:.0%})." for s in ranked_sources]
        return QueryResult(
            question=question,
            answer=f"'{best}' — {agree[best]}/{total[best]} agreements with a correct outcome.",
            explanation=explanation,
            supporting_data={"ranked": ranked_sources},
        )

    # ---------------------------------------------------------------- 8

    def market_contexts_generating_false_positives(self) -> QueryResult:
        """"Which market contexts generate false positives?" — a strategy
        matching (a non-neutral decision) whose outcome turned out
        incorrect, grouped by the market context it occurred under."""
        question = "Which market contexts generate false positives?"
        false_positives: dict[str, int] = defaultdict(int)
        totals: dict[str, int] = defaultdict(int)

        for decision in self._graph.nodes_by_type(NodeType.DECISION):
            if decision.attributes.get("simulated_action") in (None, "no_action"):
                continue
            outcome = decision.attributes.get("outcome")
            if outcome not in ("correct", "incorrect"):
                continue
            for ctx in self._graph.neighbors(decision.node_id, relation="occurred_during"):
                totals[ctx.label] += 1
                if outcome == "incorrect":
                    false_positives[ctx.label] += 1

        if not totals:
            return QueryResult(question=question, answer="Not enough resolved, non-neutral decisions with market context yet.", explanation=[])
        ranked = sorted(totals, key=lambda c: (false_positives[c] / totals[c]) if totals[c] else 0.0, reverse=True)
        worst = ranked[0]
        rate = false_positives[worst] / totals[worst]
        explanation = [f"'{c}': {false_positives[c]}/{totals[c]} matched decisions were incorrect ({(false_positives[c] / totals[c]):.0%})." for c in ranked[:5]]
        return QueryResult(
            question=question,
            answer=f"'{worst}' — {false_positives[worst]}/{totals[worst]} matched decisions turned out incorrect ({rate:.0%}).",
            explanation=explanation,
            supporting_data={"context": worst, "false_positive_rate": rate},
        )

    # ---------------------------------------------------------------- 9

    def confidence_vs_actual_outcome(self) -> QueryResult:
        """"How does confidence compare with actual outcomes?" —
        delegates to the Confidence Calibration service when available (no
        duplicated calculation); falls back to a graph-only bucketed
        comparison otherwise."""
        question = "How does confidence compare with actual outcomes?"
        if self._confidence_calibration is not None:
            report = self._confidence_calibration.report()
            explanation = ["Delegated to the Confidence Calibration service (no duplicated calculation)."]
            explanation += [f"{b.label}: predicted ~{b.expected_rate:.0%}, actual {b.actual_win_rate:.0%} ({b.sample_size} decisions) -> {b.verdict}." for b in report.buckets if b.sample_size]
            return QueryResult(
                question=question,
                answer=report.overall_verdict,
                explanation=explanation,
                supporting_data={"buckets": [b.label for b in report.buckets]},
            )

        # Graph-only fallback: bucket by decision confidence directly.
        buckets: dict[int, list[Node]] = defaultdict(list)
        for decision in self._graph.nodes_by_type(NodeType.DECISION):
            if decision.attributes.get("outcome") not in ("correct", "incorrect"):
                continue
            bucket = int(decision.attributes.get("confidence", 0.0) // 10) * 10
            buckets[bucket].append(decision)
        if not buckets:
            return QueryResult(question=question, answer="Not enough resolved decisions yet.", explanation=["Graph-only approximation (Confidence Calibration service not wired)."])
        explanation = ["Graph-only approximation (Confidence Calibration service not wired)."]
        gaps = []
        for lo in sorted(buckets):
            rate, wins, resolved = _win_rate(self._graph, buckets[lo])
            expected = (lo + 5) / 100.0
            explanation.append(f"{lo}-{lo + 10}% confidence: actual win rate {rate:.0%} over {resolved} decision(s) (expected ~{expected:.0%}).")
            gaps.append((lo, rate - expected))
        lo, gap = max(gaps, key=lambda g: abs(g[1]))
        verdict = "overconfident" if gap < 0 else "underconfident" if gap > 0 else "well-calibrated"
        return QueryResult(
            question=question,
            answer=f"Confidence is most {verdict} in the {lo}-{lo + 10}% bucket (gap {gap:+.0%}).",
            explanation=explanation,
        )

    # ---------------------------------------------------------------- platform conventions

    async def health(self) -> dict[str, Any]:
        return await self._graph.health()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "graph": self._graph.diagnostics(),
            "has_evidence_reliability": self._evidence_reliability is not None,
            "has_confidence_calibration": self._confidence_calibration is not None,
            "min_sample": self._min_sample,
        }

    def statistics(self) -> dict[str, Any]:
        return self._graph.statistics()
