"""
The Reasoning Engine.

Gathers evidence published by every plugin (Trend, Momentum, News, Macro,
Risk, Historical Patterns, ...) and synthesizes it into an explanation.
Nothing is hardcoded here about *which* plugins exist — it only ever
consumes :class:`~app.evidence.schema.Evidence` objects off the Event Bus.

The engine never issues directives ("buy", "sell"). It answers the
project's primary questions — what's happening, why, should I care, what
strategies fit, what happened historically, what risks exist, how
confident is the system — and always shows its work.

If no AI provider is configured (no API key, or ``reasoning.enabled: false``
in config), the engine still produces a useful, honest output: a
deterministic summary built directly from the evidence, clearly labeled as
evidence-only so it's never mistaken for an AI-generated thesis.

Milestone 7 extends the engine with two more inputs, both consumed purely
through the Event Bus like everything else here:

- **Market context** (``MarketContextUpdated``, from
  ``app/context/engine.py``) — the current market-environment labels for
  a symbol (and market-wide), included in both the AI prompt and the
  evidence-only fallback so a synthesis can say "in a Bull Trend, High
  Volatility environment" instead of reasoning about evidence in a
  vacuum.
- **Confidence-weighted evidence** (``EvidenceAggregated.weighted_evidence``,
  from the Confidence Weighting Framework — ``app/aggregation/weighting.py``)
  — each piece of evidence's normalized weight is attached to the AI
  payload and folded into the evidence-only summary's confidence
  calculation, without ever replacing or hiding the raw, unweighted
  evidence itself.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app.evidence.schema import Evidence
from app.event_bus.bus import EventBus
from app.event_bus.events import EvidenceAggregated, MarketContextUpdated, StrategyMatched, WeightedEvidenceEvent
from app.logging import get_logger
from app.reasoning.providers.base import ReasoningProvider

log = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are the reasoning core of an educational trading-intelligence assistant.

You are NOT a signal-selling bot. You never tell the user to buy or sell. \
You explain what is happening, why, what risks exist, and how confident you \
are — citing the specific evidence you were given by name. You help traders \
think, not replace their judgment.

You will be given a JSON list of "evidence" objects, each produced by a \
different analysis plugin (trend, momentum, news, macro, risk, historical \
patterns, etc). Combine them into a single, honest assessment. If the \
evidence is thin or conflicting, say so plainly instead of inventing \
confidence you don't have.

Respond with ONLY a single JSON object matching this exact shape, no \
markdown fences, no commentary outside the JSON:

{
  "market_summary": "what is happening, in plain language",
  "trade_thesis": "why it might matter, framed as a hypothesis to evaluate, not a directive",
  "risk_assessment": "concrete risks and what would invalidate this thesis",
  "alternative_scenario": "the other side of the trade — what if this is wrong",
  "confidence": 0-100 (integer, how much the evidence actually supports this),
  "suggested_strategies": ["names of strategy archetypes this evidence pattern fits, if any"],
  "historical_similarity": "brief note on similar historical setups, or null if none is known"
}
"""


class ReasoningOutput(BaseModel):
    market_summary: str
    trade_thesis: str
    risk_assessment: str
    alternative_scenario: str
    confidence: float = Field(ge=0, le=100)
    suggested_strategies: list[str] = Field(default_factory=list)
    historical_similarity: str | None = None
    evidence_count: int = 0
    source: str = "ai"  # "ai" | "evidence_only" | "insufficient_evidence"
    #: The Market Context Engine's current labels actually used to build
    #: this analysis -- symbol-specific and market-wide combined, keyed
    #: by context_type (e.g. {"trend": "Bull Trend", "risk_regime": "Risk-On"}).
    context: dict[str, str] = Field(default_factory=dict)


class ReasoningEngine:
    """Accumulates evidence, market context, and matched strategies per
    symbol, and synthesizes them into an explanation on demand."""

    def __init__(self, settings: Any, provider: ReasoningProvider | None = None, *, max_evidence_per_symbol: int = 50) -> None:
        self._settings = settings
        self._provider = provider
        self._max_evidence_per_symbol = max_evidence_per_symbol
        self._evidence_by_symbol: dict[str, list[Evidence]] = defaultdict(list)
        self._weighted_evidence_by_symbol: dict[str, list[WeightedEvidenceEvent]] = defaultdict(list)
        self._matched_strategies_by_symbol: dict[str, list[StrategyMatched]] = defaultdict(list)
        #: Market Context Engine labels, kept per symbol and separately
        #: for market-wide context (symbol=None on MarketContextUpdated).
        #: Known limitation, not a bug: the Context Engine only publishes
        #: on a label *changing* (see app/context/engine.py's edge-
        #: triggering), including going silent rather than announcing
        #: "back to normal" for non-trend context types -- so a context
        #: label here can go stale if the underlying condition quietly
        #: lapses. Trend always has an active label (Bull/Bear/Sideways),
        #: so it never goes stale this way.
        self._context_by_symbol: dict[str, dict[str, str]] = defaultdict(dict)
        self._market_wide_context: dict[str, str] = {}

    def attach(self, event_bus: EventBus) -> None:
        """Subscribe to the Evidence Aggregator's output — never raw
        ``EvidenceProduced`` directly, matching PROJECT.md's requirement
        that the aggregator be the single interface both the Strategy
        Engine and the Reasoning Engine consume. Also subscribes to
        ``StrategyMatched`` so a declarative strategy firing shows up in
        this engine's synthesis alongside raw evidence, and to
        ``MarketContextUpdated`` so the Market Context Engine's labels
        (Milestone 7) shape the synthesis too."""
        event_bus.subscribe(EvidenceAggregated, self._on_evidence_aggregated, name="reasoning_engine")
        event_bus.subscribe(StrategyMatched, self._on_strategy_matched, name="reasoning_engine_strategies")
        event_bus.subscribe(MarketContextUpdated, self._on_context_updated, name="reasoning_engine_context")

    async def _on_evidence_aggregated(self, event: EvidenceAggregated) -> None:
        # The aggregator already dedupes and decays evidence for us — this
        # engine always reasons over exactly the current fresh/deduped
        # snapshot for the symbol, rather than accumulating every historical
        # occurrence forever (freshness/decay is the aggregator's job, not
        # this engine's).
        symbol = event.symbol
        bucket = list(event.active_evidence)
        if len(bucket) > self._max_evidence_per_symbol:
            bucket = bucket[-self._max_evidence_per_symbol :]
        self._evidence_by_symbol[symbol] = bucket
        self._weighted_evidence_by_symbol[symbol] = list(event.weighted_evidence)

    async def _on_strategy_matched(self, event: StrategyMatched) -> None:
        bucket = self._matched_strategies_by_symbol[event.symbol]
        bucket.append(event)
        if len(bucket) > 20:
            del bucket[: len(bucket) - 20]

    async def _on_context_updated(self, event: MarketContextUpdated) -> None:
        if event.symbol is None:
            self._market_wide_context[event.context_type] = event.label
        else:
            self._context_by_symbol[event.symbol][event.context_type] = event.label

    def evidence_for(self, symbol: str) -> list[Evidence]:
        return list(self._evidence_by_symbol.get(symbol, []))

    def weighted_evidence_for(self, symbol: str) -> list[WeightedEvidenceEvent]:
        return list(self._weighted_evidence_by_symbol.get(symbol, []))

    def matched_strategies_for(self, symbol: str) -> list[StrategyMatched]:
        return list(self._matched_strategies_by_symbol.get(symbol, []))

    def context_for(self, symbol: str) -> dict[str, str]:
        """Combined symbol-specific + market-wide context, symbol-specific
        labels winning on a context_type collision (there shouldn't be
        one in practice — symbol and market-wide context_types don't
        currently overlap, but symbol-specific is the more relevant
        answer if they ever do)."""
        combined = dict(self._market_wide_context)
        combined.update(self._context_by_symbol.get(symbol, {}))
        return combined

    async def analyze(self, symbol: str) -> ReasoningOutput:
        """Answer the project's core questions for ``symbol`` from accumulated evidence."""
        evidence = self.evidence_for(symbol)

        if len(evidence) < self._settings.reasoning.min_evidence_count:
            return ReasoningOutput(
                market_summary=f"No evidence has been gathered for {symbol} yet.",
                trade_thesis="Not enough information to form a thesis.",
                risk_assessment="Unknown — insufficient data.",
                alternative_scenario="Unknown — insufficient data.",
                confidence=0,
                evidence_count=len(evidence),
                source="insufficient_evidence",
                context=self.context_for(symbol),
            )

        if self._provider is None or not self._settings.reasoning.enabled:
            return self._evidence_only_summary(symbol, evidence)

        try:
            return await self._ai_summary(symbol, evidence)
        except Exception:
            log.exception("reasoning_ai_failed_falling_back", symbol=symbol)
            return self._evidence_only_summary(symbol, evidence)

    # ---------------------------------------------------------------- AI path

    async def _ai_summary(self, symbol: str, evidence: list[Evidence]) -> ReasoningOutput:
        weight_by_id = {w.evidence.evidence_id: w.weight for w in self.weighted_evidence_for(symbol)}
        payload = []
        for e in evidence:
            item = e.model_dump(mode="json")
            item["confidence_weight"] = weight_by_id.get(e.evidence_id)
            payload.append(item)
        prompt = f"Symbol: {symbol}\n\nEvidence (each item's \"confidence_weight\" is the Confidence Weighting Framework's normalized [0,1] trust score for it, not the plugin's own confidence):\n{json.dumps(payload, indent=2)}"

        matched = self.matched_strategies_for(symbol)
        if matched:
            strategy_lines = "\n".join(f"- {m.strategy} (score {m.score}, {m.evidence_count} evidence)" for m in matched)
            prompt += f"\n\nDeclarative strategies currently matched for this symbol:\n{strategy_lines}"

        context = self.context_for(symbol)
        if context:
            context_lines = "\n".join(f"- {ctype}: {label}" for ctype, label in sorted(context.items()))
            prompt += f"\n\nCurrent market context (from the Market Context Engine):\n{context_lines}"

        raw = await self._provider.generate(
            system=_SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=self._settings.reasoning.max_tokens,
            temperature=self._settings.reasoning.temperature,
        )
        data = _extract_json(raw)
        return ReasoningOutput(
            market_summary=data["market_summary"],
            trade_thesis=data["trade_thesis"],
            risk_assessment=data["risk_assessment"],
            alternative_scenario=data["alternative_scenario"],
            confidence=float(data["confidence"]),
            suggested_strategies=list(data.get("suggested_strategies") or []),
            historical_similarity=data.get("historical_similarity"),
            evidence_count=len(evidence),
            source="ai",
            context=context,
        )

    # ---------------------------------------------------------------- fallback path

    def _evidence_only_summary(self, symbol: str, evidence: list[Evidence]) -> ReasoningOutput:
        """Deterministic, no-AI summary — used when no provider is configured
        and whenever the AI path fails, so the assistant always explains
        itself instead of going silent."""
        bullish = [e for e in evidence if e.direction == "bullish"]
        bearish = [e for e in evidence if e.direction == "bearish"]
        neutral = [e for e in evidence if e.direction == "neutral"]

        weighted = self.weighted_evidence_for(symbol)
        weight_by_id = {w.evidence.evidence_id: w.weight for w in weighted}

        if weighted:
            # Confidence Weighting Framework available -- lean and
            # confidence are computed from weighted mass, not raw counts,
            # so a handful of highly-weighted signals can outweigh a
            # larger pile of low-weight ones (see app/aggregation/weighting.py).
            weighted_mass: dict[str, float] = defaultdict(float)
            for w in weighted:
                weighted_mass[w.evidence.direction] += w.weight
            lean_source = weighted_mass
            total_weight_sum = sum(weight_by_id.get(e.evidence_id, 1.0) * e.score for e in evidence) or 1.0
            avg_confidence = sum(e.confidence * e.score * weight_by_id.get(e.evidence_id, 1.0) for e in evidence) / total_weight_sum
        else:
            lean_source = {"bullish": len(bullish), "bearish": len(bearish)}
            total_weight = sum(e.score for e in evidence) or 1.0
            avg_confidence = sum(e.confidence * e.score for e in evidence) / total_weight

        if lean_source.get("bullish", 0) > lean_source.get("bearish", 0):
            lean = "bullish"
        elif lean_source.get("bearish", 0) > lean_source.get("bullish", 0):
            lean = "bearish"
        else:
            lean = "mixed"

        titles = ", ".join(f"{e.source}: {e.title}" for e in evidence[-5:])
        summary = (
            f"{symbol} has {len(evidence)} piece(s) of evidence "
            f"({len(bullish)} bullish, {len(bearish)} bearish, {len(neutral)} neutral). "
            f"Overall lean: {lean}. Most recent: {titles}."
        )

        context = self.context_for(symbol)
        if context:
            context_bits = ", ".join(f"{label}" for label in context.values())
            summary += f" Current market context: {context_bits}."

        matched = self.matched_strategies_for(symbol)
        if matched:
            names = ", ".join(f"{m.strategy} (score {m.score})" for m in matched)
            summary += f" Matched strategies: {names}."

        return ReasoningOutput(
            market_summary=summary,
            trade_thesis=(
                "AI synthesis is unavailable (no provider configured or the AI call failed) — "
                "this is a raw aggregation of plugin evidence only, not an interpreted thesis."
            ),
            risk_assessment="Not assessed — configure ANTHROPIC_API_KEY to enable full risk analysis.",
            alternative_scenario="Not assessed — evidence-only mode.",
            confidence=round(avg_confidence, 2),
            suggested_strategies=[m.strategy for m in matched],
            historical_similarity=None,
            evidence_count=len(evidence),
            source="evidence_only",
            context=context,
        )


def _extract_json(raw: str) -> dict[str, Any]:
    """Parse the provider's response as JSON, tolerating stray markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
