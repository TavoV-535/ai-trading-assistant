"""
The Adaptive Risk Profile system.

Per PROJECT.md's Milestone 11 spec: Risk Profiles define configurable
operating constraints — Conservative, Swing Trader, Day Trader, Scalper,
Prop Firm, and Custom Profiles — each configuring a maximum daily loss,
maximum total drawdown, maximum position size, maximum concurrent
positions, maximum portfolio exposure, correlation limits, sector limits,
symbol limits, and a future leverage limit placeholder. The Capital
Protection Engine (``app/capital_protection/engine.py``) must consume
these dynamically, without requiring code changes.

The schema itself (:class:`~app.config.settings.RiskProfileConfig`) lives
in ``app/config/settings.py``, alongside every other configuration
section in this codebase — Configuration over code, one obvious place to
look for what a profile can configure. :class:`RiskProfileRegistry` is
the live, queryable wrapper around ``settings.capital_protection``: which
profiles exist, which one is active, and the one runtime mutation this
system supports — switching (or registering a brand new Custom Profile)
without editing a single line of the engine that consumes it.
"""
from __future__ import annotations

from typing import Any

from app.config.settings import RiskProfileConfig
from app.logging import get_logger

log = get_logger(__name__)

#: Every consumer imports ``RiskProfile`` from here, not
#: ``app.config.settings`` — "where do Risk Profiles live" has one
#: obvious answer, even though the schema itself is defined alongside
#: every other config section for consistency with this codebase's
#: established ``settings.py`` convention (see that module's docstring).
RiskProfile = RiskProfileConfig


class RiskProfileRegistry:
    """The live, queryable set of configured Risk Profiles plus which one
    is currently active. Constructed once from ``settings.capital_protection``
    (mirroring every other core engine's ``settings`` constructor
    parameter) — the Capital Protection Engine always asks this registry
    for "the current profile" and never hardcodes a profile's name or
    limits, which is what makes "profile switching without code
    modifications" possible at all."""

    def __init__(self, settings: Any) -> None:
        section = getattr(settings, "capital_protection", None)
        self._profiles: dict[str, RiskProfile] = dict(getattr(section, "profiles", None) or {})
        configured_active = str(getattr(section, "active_profile", "") or "")
        self._active_name = configured_active if configured_active in self._profiles else next(iter(self._profiles), "")
        if not self._profiles:
            log.warning(
                "risk_profile_registry_empty",
                detail="No profiles configured -- see capital_protection.profiles in config/default.yaml",
            )

    # ---------------------------------------------------------------- queries

    def names(self) -> list[str]:
        return sorted(self._profiles.keys())

    def get(self, name: str) -> RiskProfile | None:
        return self._profiles.get(name)

    @property
    def active_name(self) -> str:
        return self._active_name

    def current(self) -> RiskProfile:
        """The active profile. Falls back to a hardcoded-safe, clearly
        named ``"fallback"`` profile only if this registry was
        constructed with zero configured profiles (a misconfiguration,
        not a normal path) — never raises, so the Capital Protection
        Engine can always evaluate against *something* (graceful
        degradation, the same convention every other core engine in this
        codebase follows for a missing/empty config section)."""
        profile = self._profiles.get(self._active_name)
        if profile is not None:
            return profile
        return RiskProfile(name="fallback")

    # ---------------------------------------------------------------- mutation

    def set_active(self, name: str) -> bool:
        """Switches the active profile — the entire mechanism behind
        "profile switching without code modifications": a ``/risk``
        command parameter (or any future caller) changes *which
        already-configured profile* is active; nothing ever edits limits
        in code to do it. Returns whether the switch succeeded; an
        unknown name is a logged no-op (the previous profile stays
        active) rather than raising."""
        if name not in self._profiles:
            log.warning("risk_profile_switch_unknown", requested=name, known=self.names())
            return False
        self._active_name = name
        log.info("risk_profile_switched", active_profile=name)
        return True

    def register(self, profile: RiskProfile, *, activate: bool = False) -> None:
        """Adds (or replaces) a Custom Profile at runtime — the "Custom
        Profiles" requirement in PROJECT.md's Milestone 11 spec, without
        needing a config file edit and restart. ``activate=True`` also
        makes it the active profile immediately, in the same call."""
        self._profiles[profile.name] = profile
        log.info("risk_profile_registered", name=profile.name, activate=activate)
        if activate:
            self._active_name = profile.name
