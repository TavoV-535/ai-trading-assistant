from __future__ import annotations

from app.config import get_settings


def test_defaults_load_from_yaml(settings):
    assert settings.app.name == "AI Trading Assistant"
    assert settings.logging.level == "INFO"
    assert "plugins/indicators" in settings.plugins.search_paths
    assert settings.reasoning.model == "claude-opus-4-8"


def test_env_var_overrides_yaml(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("REASONING_MODEL", "claude-sonnet-5")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.logging.level == "DEBUG"
    assert settings.reasoning.model == "claude-sonnet-5"


def test_secrets_default_to_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
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
