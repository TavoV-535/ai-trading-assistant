"""Unit tests for the Adaptive Risk Profile system
(``app/capital_protection/profiles.py``)."""
from __future__ import annotations

from app.capital_protection.profiles import RiskProfile, RiskProfileRegistry


def test_registry_loads_built_in_profiles_from_settings(settings):
    registry = RiskProfileRegistry(settings)
    assert registry.names() == ["conservative", "day_trader", "prop_firm", "scalper", "swing_trader"]


def test_registry_defaults_to_configured_active_profile(settings):
    registry = RiskProfileRegistry(settings)
    assert registry.active_name == settings.capital_protection.active_profile
    assert registry.current().name == settings.capital_protection.active_profile


def test_set_active_switches_the_current_profile(settings):
    registry = RiskProfileRegistry(settings)
    assert registry.set_active("prop_firm") is True
    assert registry.active_name == "prop_firm"
    assert registry.current().name == "prop_firm"
    assert registry.current().max_daily_loss_pct == settings.capital_protection.profiles["prop_firm"].max_daily_loss_pct


def test_set_active_unknown_profile_is_a_no_op(settings):
    registry = RiskProfileRegistry(settings)
    before = registry.active_name
    assert registry.set_active("does_not_exist") is False
    assert registry.active_name == before


def test_register_adds_a_custom_profile_without_activating_by_default(settings):
    registry = RiskProfileRegistry(settings)
    custom = RiskProfile(name="my_custom_profile", max_daily_loss_pct=1.0, max_total_drawdown_pct=2.0)
    registry.register(custom)

    assert "my_custom_profile" in registry.names()
    assert registry.get("my_custom_profile") == custom
    assert registry.active_name != "my_custom_profile"  # not activated unless asked


def test_register_with_activate_true_makes_it_current(settings):
    registry = RiskProfileRegistry(settings)
    custom = RiskProfile(name="my_custom_profile", max_daily_loss_pct=1.0)
    registry.register(custom, activate=True)

    assert registry.active_name == "my_custom_profile"
    assert registry.current().name == "my_custom_profile"


def test_register_replaces_an_existing_profile_of_the_same_name(settings):
    registry = RiskProfileRegistry(settings)
    replacement = RiskProfile(name="conservative", max_daily_loss_pct=99.0)
    registry.register(replacement)

    assert registry.get("conservative").max_daily_loss_pct == 99.0


def test_current_falls_back_to_a_safe_default_when_registry_is_empty():
    class _EmptySection:
        profiles: dict = {}
        active_profile = "nonexistent"

    class _Settings:
        capital_protection = _EmptySection()

    registry = RiskProfileRegistry(_Settings())
    profile = registry.current()
    assert profile.name == "fallback"
    assert registry.names() == []
