"""
The Clock abstraction.

Every core engine that computes something time-sensitive (evidence
freshness/decay, a confidence trend over a rolling window, an alert
cooldown or alert-suppression window) needs to answer one question: "what
time is it right now?" During live operation the honest answer is always
``datetime.now(timezone.utc)``. During a Simulation Engine run
(``app/simulation/``) replaying historical data, the honest answer is "the
timestamp of the historical bar currently being processed" — using the
real wall clock instead would make freshness/decay/cooldown math depend on
how fast the simulation happens to execute on this particular machine on
this particular run, breaking the platform's Milestone 9 determinism
requirement ("given identical historical data and configuration, repeated
simulations should produce identical results").

``EvidenceAggregator``, ``MarketContextEngine``, ``StrategyEngine``,
``PortfolioIntelligenceEngine``, and ``EventPrioritizationEngine`` all
accept an optional ``clock: Clock`` constructor parameter (default
``SystemClock()``, so every existing call site and every existing test is
completely unaffected). The Simulation Engine constructs each of them
with one shared ``SimulatedClock`` per run and advances it to match the
current simulated bar before publishing that bar's ``MarketDataUpdated``
event — so every engine's internal "now" is consistent with, and only
with, the simulated timeline, never the real one.

Just as important: every event one of these five engines *publishes*
(``EvidenceAggregated``, ``MarketContextUpdated``, ``StrategyMatched``,
``SymbolProfileUpdated``, ``AlertGenerated``) is stamped with
``timestamp=self._clock.now()`` explicitly at the publish call site,
rather than left to ``Event``'s own ``default_factory=datetime.now``. This
isn't optional polish — a downstream engine's own internal "now" being
simulated doesn't help if the *event* it emits still carries a real
wall-clock timestamp; two runs with identical simulated data would then
produce identical decisions but non-identical event timestamps, which
already fails "identical event sequences" literally. (This exact gap was
caught by ``tests/test_simulation_engine.py::
test_simulation_determinism_two_independent_runs_match`` comparing two
runs' ``AlertGenerated`` events — see that test and this fix's commit for
the full story.)

Deliberately NOT propagated into indicator/intelligence plugins'
``Evidence.created_at`` or ``IndicatorCalculated``/``EvidenceProduced``
timestamps — nothing in this codebase's decision logic ever reads those
fields (verified: only ``EventLogRepository.recent()`` orders by
``EventLog.created_at``, a DB write-time audit column, not a plugin-set
value). Simulated runs are fully deterministic in every value that
actually feeds a decision — evidence content, weights, matched strategies,
alert scores, reasoning output, and now every core-engine-published
event's own timestamp — even though a plugin's own cosmetic
``Evidence.created_at`` stamp still reflects real wall-clock time, exactly
as it does live. This is a deliberate, narrow, documented scope boundary,
not a silent gap.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone


class Clock(ABC):
    """Anything that can answer "what time is it right now?"."""

    @abstractmethod
    def now(self) -> datetime:
        """The current time, always timezone-aware UTC."""


class SystemClock(Clock):
    """The real wall clock — used everywhere by default, live operation
    included. Stateless; safe to share or construct freely."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SimulatedClock(Clock):
    """A settable virtual clock, owned and advanced only by the Simulation
    Engine for the lifetime of one run. Time only ever moves forward —
    every core engine that reads it assumes a monotonically increasing
    "now", the same guarantee the real wall clock provides."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2020, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        """Move the clock forward to ``when``. Raises if ``when`` is
        earlier than the current simulated time — a backwards jump would
        silently corrupt every freshness/decay/cooldown calculation
        downstream engines have already made against the old value."""
        if when < self._now:
            raise ValueError(f"SimulatedClock cannot move backwards: currently {self._now}, asked for {when}")
        self._now = when

    def tick(self, delta: timedelta) -> datetime:
        """Advance by ``delta`` and return the new current time —
        convenience wrapper around ``advance_to`` for callers stepping by a
        fixed bar interval rather than an absolute timestamp."""
        self.advance_to(self._now + delta)
        return self._now
