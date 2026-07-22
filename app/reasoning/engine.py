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
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from app.evidence.schema import Evidence
from app.event_bus.bus import EventBus
from app.event_bus.events import EvidenceAggregated, StrategyMatched
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


class ReasoningEngine:
    """Accumulates evidence per symbol and synthesizes it on demand."""

    def __init__(self, settings: Any, provider: ReasoningProvider | None = None, *, max_evidence_per_symbol: int = 50) -> None:
        self._settings = settings
        self._provider = provider
        self._max_evidence_per_symbol = max_evidence_per_symbol
        self._evidence_by_symbol: dict[str, list[Evidence]] = defaultdict(list)
        self._matched_strategies_by_symbol: dict[str, list[StrategyMatched]] = defaultdict(list)

    def attach(self, event_bus: EventBus) -> None:
        """Subscribe to the Evidence Aggregator's output — never raw
        ``EvidenceProduced`` directly, matching PROJECT.md's requirement
        that the aggregator be the single interface both the Strategy
        Engine and the Reasoning Engine consume. Also subscribes to
        ``StrategyMatched`` so a declarative strategy firing shows up in
        this engine's synthesis alongside raw evidence."""
        event_bus.subscribe(EvidenceAggregated, self._on_evidence_aggregated, name="reasoning_engine")
        event_bus.subscribe(StrategyMatched, self._on_strategy_matched, name="reasoning_engine_strategies")

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

    async def _on_strategy_matched(self, event: StrategyMatched) -> None:
        bucket = self._matched_strategies_by_symbol[event.symbol]
        bucket.append(event)
        if len(bucket) > 20:
            del bucket[: len(bucket) - 20]

    def evidence_for(self, symbol: str) -> list[Evidence]:
        return list(self._evidence_by_symbol.get(symbol, []))

    def matched_strategies_for(self, symbol: str) -> list[StrategyMatched]:
        return list(self._matched_strategies_by_symbol.get(symbol, []))

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
        payload = [e.model_dump(mode="json") for e in evidence]
        prompt = f"Symbol: {symbol}\n\nEvidence:\n{json.dumps(payload, indent=2)}"

        matched = self.matched_strategies_for(symbol)
        if matched:
            strategy_lines = "\n".join(f"- {m.strategy} (score {m.score}, {m.evidence_count} evidence)" for m in matched)
            prompt += f"\n\nDeclarative strategies currently matched for this symbol:\n{strategy_lines}"

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
        )

    # ---------------------------------------------------------------- fallback path

    def _evidence_only_summary(self, symbol: str, evidence: list[Evidence]) -> ReasoningOutput:
        """Deterministic, no-AI summary — used when no provider is configured
        and whenever the AI path fails, so the assistant always explains
        itself instead of going silent."""
        bullish = [e for e in evidence if e.direction == "bullish"]
        bearish = [e for e in evidence if e.direction == "bearish"]
        neutral = [e for e in evidence if e.direction == "neutral"]

        total_weight = sum(e.score for e in evidence) or 1.0
        avg_confidence = sum(e.confidence * e.score for e in evidence) / total_weight

        if len(bullish) > len(bearish):
            lean = "bullish"
        elif len(bearish) > len(bullish):
            lean = "bearish"
        else:
            lean = "mixed"

        titles = ", ".join(f"{e.source}: {e.title}" for e in evidence[-5:])
        summary = (
            f"{symbol} has {len(evidence)} piece(s) of evidence "
            f"({len(bullish)} bullish, {len(bearish)} bearish, {len(neutral)} neutral). "
            f"Overall lean: {lean}. Most recent: {titles}."
        )

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
        )


def _extract_json(raw: str) -> dict[str, Any]:
    """Parse the provider's response as JSON, tolerating stray markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
