from __future__ import annotations

from app.config import get_settings


def test_defaults_load_from_yaml(settings):
    assert settings.app.name == "AI Trading Assistant"
    assert settings.logging.level == "INFO"
    assert "plugins/indicators" in settings.plugins.search_paths
    assert settings.reasoning.model == "claude-opus-4-8"


def test_milestone7_sections_load_from_yaml(settings):
    assert "plugins/intelligence" in settings.plugins.search_paths
    assert "plugins/news" not in settings.plugins.search_paths  # replaced by the unified platform
    assert settings.intelligence.interval_seconds > 0
    assert settings.context.trend_window == 20
    assert settings.context.trend_bull_threshold_pct > 0
    assert settings.confidence_weighting.source_reliability["EMA"] == 0.75
    assert settings.confidence_weighting.regime_aligned_boost > 1.0


def test_env_var_overrides_yaml(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("REASONING_MODEL", "claude-sonnet-5")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.logging.level == "DEBUG"
    assert settings.reasoning.model == "claude-sonnet-5"


def test_secrets_default_to_none(monkeypatch):
    # The autouse `_isolated_settings` fixture (conftest.py) already shadows
    # these with "" so this test doesn't depend on whether a real .env
    # happens to exist in the project root — `delenv` alone wouldn't be
    # enough since a real .env file is a separate, lower-priority settings
    # source that `delenv` can't reach.
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.has_anthropic_key is False
    assert settings.has_discord_token is False


def test_anthropic_key_detected_when_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.has_anthropic_key is True
    assert settings.anthropic_api_key.get_secret_value() == "sk-test-key"


def test_database_url_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg://")
