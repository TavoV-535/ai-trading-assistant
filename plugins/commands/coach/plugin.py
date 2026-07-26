"""
/coach [focus] — the Learning Engine's coaching insights, on demand.

The Milestone 12 spec's completion checklist asks for "/coach producing
actionable recommendations backed by historical evidence." This command
is a pure read surface over three already-built Milestone 12 engines —
``context.learning_engine`` (published ``CoachingEvent`` history),
``context.analytics_service`` (Strategy Analytics + Confidence
Calibration, composed, never recalculated here), and
``context.knowledge_graph_query`` (used only for the "recommended next
focus" fallback when no coaching event exists yet) — never a fourth
calculation path. Exactly like the Learning Engine itself, this command
never alters history: it only reads and renders.

Two modes, both against the same command:

- **Full summary** (no ``focus`` given): one concise pass over every
  capability the Milestone 12 spec lists for ``/coach`` — overall
  performance summary, recent coaching events, historical improvements,
  recurring mistakes, best strengths, confidence calibration, recommended
  next focus, trend over time, top strategies, and risk observations —
  each rendered as a short block so the whole response stays scannable.
- **Focused deep-dive** (``focus`` given, e.g. ``mistakes``,
  ``strengths``, ``calibration``, ``strategies``, ``risk``, ``trend``,
  ``events``): expands just that one section with the full detail (every
  matching ``CoachingEvent``'s evidence, supporting history, and suggested
  improvements) instead of the compact one-liner the full summary uses.
  An unrecognized ``focus`` value is reported back with the list of valid
  ones, never silently ignored.

The Action Registry's pre-existing "coach" button (already registered —
see ``app/discord/actions.py`` — but never given a real handler) is left
on the generic placeholder for the same reason ``/journal`` leaves
"journal" alone: a button callback only receives ``(interaction,
target)``, with no ``PluginContext``/live-engine access, so wiring a real
handler for it would need a larger structural change this milestone's
spec didn't ask for. This command is the supported way to reach the
Learning Engine's coaching output today.
"""
from __future__ import annotations

from typing import Any

from app.analytics.calibration import CalibrationReport
from app.analytics.strategy_analytics import StrategyStats
from app.discord.actions import ACTION_REGISTRY
from app.discord.command_plugin import CommandOption, DiscordCommandPlugin
from app.discord.dispatch import CommandContext, CommandResponse
from app.event_bus.events import CoachingEvent
from app.logging import get_logger
from app.plugins.base import PluginHealth, PluginPermission

log = get_logger(__name__)

_ACTIONS = ["refresh", "dismiss"]

#: How many recent coaching events feed the full-summary sections below.
#: The full, unbounded published history remains queryable through the
#: Event Bus / durable event log directly (every CoachingEvent is logged
#: like any other event) -- this is just what keeps one Discord message
#: readable, the same bounded-render pattern /journal already uses.
_RECENT_EVENTS_LIMIT = 20
_TOP_STRATEGIES_SHOWN = 3

_MISTAKE_PATTERNS = {"recurring_mistake"}
_STRENGTH_PATTERNS = {"recurring_strength", "strongest_strategy", "strongest_evidence_combination"}
_RISK_PATTERNS = {
    "risk_management_habit",
    "stop_loss_behavior",
    "profit_taking_behavior",
    "overtrading",
    "undertrading",
}
_TREND_PATTERNS = {
    "hold_time_trend",
    "volatility_regime_performance",
    "time_of_day_performance",
    "day_of_week_performance",
    "market_session_performance",
    "watchlist_performance_trend",
    "emotional_trend",
}

_FOCUS_CHOICES = ("summary", "events", "mistakes", "strengths", "calibration", "strategies", "risk", "trend")


class CoachPlugin(DiscordCommandPlugin):
    """Reads the Learning Engine's coaching history and the Analytics
    Service's strategy/calibration stats -- never recalculates either."""

    name = "Coach"
    version = "0.1.0"
    category = "commands"
    command_name = "coach"
    command_description = "Coaching insights: performance, recurring mistakes/strengths, calibration, and more."
    parameters = (
        CommandOption(
            name="focus",
            description="Optional: summary, events, mistakes, strengths, calibration, strategies, risk, trend",
            required=False,
        ),
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._invocations = 0

    async def initialize(self) -> None:
        log.info("coach_plugin_initialized")

    async def shutdown(self) -> None:
        log.info("coach_plugin_shutdown", invocations=self._invocations)

    async def health(self) -> PluginHealth:
        return PluginHealth(status="healthy", detail=f"{self._invocations} invocation(s)")

    def config(self) -> dict[str, Any]:
        return {}

    def permissions(self) -> list[str]:
        return [PluginPermission.EVENTS_PUBLISH]

    async def execute(self, ctx: CommandContext) -> CommandResponse:
        self._invocations += 1

        learning_engine = self.context.learning_engine
        analytics_service = self.context.analytics_service
        if learning_engine is None or analytics_service is None:
            return CommandResponse(
                content="Coaching isn't available right now — the Learning Engine isn't wired up.",
                ephemeral=True,
            )

        focus = str(ctx.args.get("focus") or "").strip().lower()
        if focus and focus not in _FOCUS_CHOICES:
            return CommandResponse(
                content=f"Unknown focus '{focus}'. Valid options: {', '.join(_FOCUS_CHOICES)}.",
                ephemeral=True,
            )

        events = learning_engine.recent_coaching_events(limit=_RECENT_EVENTS_LIMIT)
        strategy_stats = analytics_service.all_strategy_stats()
        calibration = analytics_service.calibration_report()

        if focus in ("", "summary"):
            content = _full_summary(learning_engine, events, strategy_stats, calibration)
        elif focus == "events":
            content = _focus_events(events)
        elif focus == "mistakes":
            content = _focus_pattern_group(events, _MISTAKE_PATTERNS, "Recurring mistakes")
        elif focus == "strengths":
            content = _focus_pattern_group(events, _STRENGTH_PATTERNS, "Best strengths")
        elif focus == "calibration":
            content = _focus_calibration(calibration)
        elif focus == "strategies":
            content = _focus_strategies(strategy_stats)
        elif focus == "risk":
            content = _focus_pattern_group(events, _RISK_PATTERNS, "Risk observations")
        else:  # focus == "trend"
            content = _focus_trend(events, strategy_stats)

        return CommandResponse(content=content, buttons=ACTION_REGISTRY.buttons_for(_ACTIONS, target="coach"))


# ---------------------------------------------------------------- rendering


def _full_summary(
    learning_engine: Any,
    events: list[CoachingEvent],
    strategy_stats: list[StrategyStats],
    calibration: CalibrationReport,
) -> str:
    stats = learning_engine.statistics()
    lines = [
        "**Coach**",
        "",
        f"**Overall performance summary** — {stats['total_reviews']} review(s) run, "
        f"{stats['coaching_events_published']} coaching event(s) published so far.",
        "",
    ]

    lines.append("**Top strategies**")
    lines.extend(_top_strategy_lines(strategy_stats) or ["  No strategy history yet."])
    lines.append("")

    lines.append("**Recent coaching events**")
    lines.extend(_event_one_liners(events[-5:]) or ["  None published yet."])
    lines.append("")

    lines.append("**Historical improvements**")
    lines.extend(_bulleted(_historical_improvements(events)) or ["  Nothing suggested yet."])
    lines.append("")

    mistakes = _matching(events, _MISTAKE_PATTERNS)
    lines.append("**Recurring mistakes**")
    lines.extend(_event_one_liners(mistakes) or ["  None detected yet."])
    lines.append("")

    strengths = _matching(events, _STRENGTH_PATTERNS)
    lines.append("**Best strengths**")
    lines.extend(_event_one_liners(strengths) or ["  None detected yet."])
    lines.append("")

    lines.append("**Confidence calibration**")
    lines.append(f"  {calibration.overall_verdict}")
    lines.append("")

    risk_observations = _matching(events, _RISK_PATTERNS)
    lines.append("**Risk observations**")
    lines.extend(_event_one_liners(risk_observations) or ["  None detected yet."])
    lines.append("")

    trend_events = _matching(events, _TREND_PATTERNS)
    lines.append("**Trend over time**")
    lines.extend(_event_one_liners(trend_events) or ["  Not enough history yet to call a trend."])
    lines.append("")

    recommendation = _recommended_next_focus(events)
    lines.append("**Recommended next focus**")
    lines.append(f"  {recommendation}")
    lines.append("")

    lines.append(f"_Use `/coach focus:<{'|'.join(_FOCUS_CHOICES[1:])}>` to drill into one section._")
    return "\n".join(lines).rstrip()


def _focus_events(events: list[CoachingEvent]) -> str:
    if not events:
        return "**Recent coaching events**\n\nNone published yet — the Learning Engine reviews behavior automatically as decisions resolve (see `/coach` for the review cadence)."
    lines = [f"**Recent coaching events** _({len(events)})_", ""]
    for event in reversed(events):
        lines.extend(_event_detail_lines(event))
        lines.append("")
    return "\n".join(lines).rstrip()


def _focus_pattern_group(events: list[CoachingEvent], patterns: set[str], title: str) -> str:
    matches = _matching(events, patterns)
    if not matches:
        return f"**{title}**\n\nNone detected yet — needs more resolved history to find a pattern."
    lines = [f"**{title}** _({len(matches)})_", ""]
    for event in reversed(matches):
        lines.extend(_event_detail_lines(event))
        lines.append("")
    return "\n".join(lines).rstrip()


def _focus_calibration(calibration: CalibrationReport) -> str:
    if not calibration.buckets:
        return f"**Confidence calibration**\n\n{calibration.overall_verdict}"
    lines = ["**Confidence calibration**", "", calibration.overall_verdict, ""]
    for bucket in calibration.buckets:
        lines.append(
            f"  {bucket.label}: expected {bucket.expected_rate:.0%}, actual {bucket.actual_win_rate:.0%} "
            f"_(n={bucket.sample_size}, {bucket.verdict})_"
        )
    return "\n".join(lines).rstrip()


def _focus_strategies(strategy_stats: list[StrategyStats]) -> str:
    if not strategy_stats:
        return "**Top strategies**\n\nNo strategy history yet."
    ranked = sorted(strategy_stats, key=lambda s: (s.win_rate, s.sample_size), reverse=True)
    lines = ["**Strategies (ranked by win rate)**", ""]
    for s in ranked:
        pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a"
        exp = f"{s.expectancy:.2f}" if s.expectancy is not None else "n/a"
        lines.append(
            f"  **{s.strategy}** — win rate {s.win_rate:.0%} _(n={s.sample_size})_, "
            f"profit factor {pf}, expectancy {exp}, trend {s.historical_trend}"
        )
    return "\n".join(lines).rstrip()


def _focus_trend(events: list[CoachingEvent], strategy_stats: list[StrategyStats]) -> str:
    lines = ["**Trend over time**", ""]
    trend_events = _matching(events, _TREND_PATTERNS)
    if trend_events:
        for event in reversed(trend_events):
            lines.extend(_event_detail_lines(event))
            lines.append("")
    else:
        lines.append("  No trend-pattern coaching events yet.")
        lines.append("")

    trending_strategies = [s for s in strategy_stats if s.historical_trend in ("improving", "declining")]
    if trending_strategies:
        lines.append("**Per-strategy win-rate trend**")
        for s in sorted(trending_strategies, key=lambda s: s.strategy):
            lines.append(f"  {s.strategy}: {s.historical_trend} _(n={s.sample_size})_")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------- helpers


def _matching(events: list[CoachingEvent], patterns: set[str]) -> list[CoachingEvent]:
    return [e for e in events if e.pattern_type in patterns]


def _top_strategy_lines(strategy_stats: list[StrategyStats]) -> list[str]:
    ranked = sorted(strategy_stats, key=lambda s: (s.win_rate, s.sample_size), reverse=True)
    lines = []
    for s in ranked[:_TOP_STRATEGIES_SHOWN]:
        lines.append(f"  **{s.strategy}** — win rate {s.win_rate:.0%} _(n={s.sample_size})_")
    return lines


def _event_one_liners(events: list[CoachingEvent]) -> list[str]:
    return [f"  [{e.priority}] {e.title}" for e in reversed(events)]


def _event_detail_lines(event: CoachingEvent) -> list[str]:
    lines = [f"**[{event.priority}] {event.title}** _(confidence {event.confidence:.0f}/100, seen {event.historical_frequency}x)_"]
    if event.summary:
        lines.append(f"  {event.summary}")
    if event.evidence:
        lines.append(f"  Evidence: {'; '.join(event.evidence)}")
    if event.supporting_history:
        lines.append(f"  History: {'; '.join(event.supporting_history)}")
    if event.suggested_improvements:
        lines.append(f"  Suggested improvements: {'; '.join(event.suggested_improvements)}")
    if event.related_strategies:
        lines.append(f"  Related strategies: {', '.join(event.related_strategies)}")
    if event.related_market_contexts:
        lines.append(f"  Related market contexts: {', '.join(event.related_market_contexts)}")
    return lines


def _historical_improvements(events: list[CoachingEvent]) -> list[str]:
    """Every suggested improvement across recent coaching events,
    deduplicated but order-preserving (most recently seen first) -- this
    command's read of "historical improvements" from the Milestone 12
    spec's ``/coach`` capability list."""
    seen: dict[str, None] = {}
    for event in reversed(events):
        for improvement in event.suggested_improvements:
            seen.setdefault(improvement, None)
    return list(seen.keys())


def _recommended_next_focus(events: list[CoachingEvent]) -> str:
    if not events:
        return "Not enough history yet — keep trading/simulating so the Learning Engine has resolved decisions to review."
    priority_rank = {"high": 2, "medium": 1, "low": 0}
    best = max(events, key=lambda e: (priority_rank.get(e.priority, 0), events.index(e)))
    return f"[{best.priority}] {best.title} — {best.summary}" if best.summary else f"[{best.priority}] {best.title}"


def _bulleted(items: list[str]) -> list[str]:
    return [f"  - {item}" for item in items]
