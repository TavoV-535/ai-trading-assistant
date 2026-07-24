"""
Configuration over code.

Non-secret behavior lives in ``config/default.yaml``. Secrets and
per-environment overrides live in ``.env`` / real environment variables.
Precedence (highest wins): environment variables > .env file > YAML file
> field defaults declared below.

Nothing in this codebase should read `os.environ` directly — always go
through :func:`get_settings`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"


class AppSection(BaseModel):
    name: str = "AI Trading Assistant"
    env: str = "development"
    timezone: str = "America/New_York"


class LoggingSection(BaseModel):
    level: str = "INFO"
    json_logs: bool = Field(default=False, alias="json")
    log_dir: str = "logs"
    max_bytes: int = 10_485_760
    backup_count: int = 5

    model_config = SettingsConfigDict(populate_by_name=True)


class EventBusSection(BaseModel):
    queue_max_size: int = 1000
    slow_handler_threshold: float = 2.0


class PluginsSection(BaseModel):
    search_paths: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class ReasoningSection(BaseModel):
    enabled: bool = True
    provider: str = "anthropic"
    model: str = "claude-opus-4-8"
    max_tokens: int = 1500
    temperature: float = 0.3
    min_evidence_count: int = 1


class DatabaseSection(BaseModel):
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False


class ScannerSection(BaseModel):
    interval_seconds: int = 60
    timeframes: list[str] = Field(default_factory=list)
    asset_classes: list[str] = Field(default_factory=list)


class ApiSection(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class DiscordSection(BaseModel):
    command_prefix: str = "/"


class MarketDataSection(BaseModel):
    #: Provider names to try, in priority order (matched against each
    #: MarketDataProviderPlugin's ``provider_name`` -- see
    #: app/marketdata/service.py). A symbol/timeframe not served by the
    #: first provider falls over to the next configured one instead of
    #: failing the whole fetch.
    providers: list[str] = Field(default_factory=lambda: ["replay"])


class AggregationSection(BaseModel):
    #: How long a piece of evidence is considered "fresh" before it decays
    #: out of the Strategy Engine's / Reasoning Engine's active view. See
    #: app/aggregation/aggregator.py.
    freshness_window_seconds: float = 900.0
    #: Bounded per-symbol history retained by the Evidence Aggregator
    #: (preserves the full historical sequence, not just the fresh subset).
    max_history_per_symbol: int = 500


class IntelligenceSection(BaseModel):
    #: Default poll interval for every External Intelligence Platform
    #: plugin (News, Earnings, Macro, ...) that doesn't set its own
    #: ``interval_seconds`` in its ``config.yaml``. See
    #: app/intelligence/plugin.py.
    interval_seconds: int = 60


class ContextSection(BaseModel):
    """Tunable thresholds for the Market Context Engine
    (``app/context/engine.py``). Every derivation it makes reads its
    threshold from here rather than a hardcoded constant, so an operator
    can retune what counts as "trending" or "high volatility" for a given
    asset class without touching code."""

    trend_window: int = 20
    trend_bull_threshold_pct: float = 1.5
    trend_bear_threshold_pct: float = -1.5
    volatility_window: int = 20
    high_volatility_threshold_pct: float = 2.0
    low_volatility_threshold_pct: float = 0.3
    gap_threshold_pct: float = 2.0
    low_liquidity_volume_ratio: float = 0.4
    risk_regime_min_symbols: int = 2
    risk_regime_majority_ratio: float = 0.6


class ConfidenceWeightingSection(BaseModel):
    """Tunable inputs for the Confidence Weighting Framework
    (``app/aggregation/weighting.py``). ``source_reliability`` maps an
    evidence ``source`` name (e.g. ``"EMA"``, ``"News"``, ``"Macro"``) to
    a ``[0, 1]`` reliability multiplier; any source not listed falls back
    to ``app.aggregation.weighting.DEFAULT_SOURCE_RELIABILITY``."""

    source_reliability: dict[str, float] = Field(default_factory=dict)
    max_cross_confirmation_boost: float = 0.5
    max_timeframe_alignment_boost: float = 0.2
    contradiction_penalty: float = 0.5
    regime_aligned_boost: float = 1.2
    regime_opposed_penalty: float = 0.85


class Settings(BaseSettings):
    """Root settings object. Instantiate via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- secrets & environment-only values ----------
    app_env: str = Field(default="development", alias="APP_ENV")
    log_level_override: str | None = Field(default=None, alias="LOG_LEVEL")
    database_url: str = Field(
        default="postgresql+asyncpg://trading:trading@localhost:5432/trading_assistant",
        alias="DATABASE_URL",
    )
    discord_bot_token: SecretStr | None = Field(default=None, alias="DISCORD_BOT_TOKEN")
    discord_guild_id: str | None = Field(default=None, alias="DISCORD_GUILD_ID")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    reasoning_model_override: str | None = Field(default=None, alias="REASONING_MODEL")
    polygon_api_key: SecretStr | None = Field(default=None, alias="POLYGON_API_KEY")
    finnhub_api_key: SecretStr | None = Field(default=None, alias="FINNHUB_API_KEY")

    # ---------- non-secret behavior, sourced from YAML ----------
    app: AppSection = Field(default_factory=AppSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    event_bus: EventBusSection = Field(default_factory=EventBusSection)
    plugins: PluginsSection = Field(default_factory=PluginsSection)
    reasoning: ReasoningSection = Field(default_factory=ReasoningSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    scanner: ScannerSection = Field(default_factory=ScannerSection)
    api: ApiSection = Field(default_factory=ApiSection)
    discord: DiscordSection = Field(default_factory=DiscordSection)
    aggregation: AggregationSection = Field(default_factory=AggregationSection)
    market_data: MarketDataSection = Field(default_factory=MarketDataSection)
    intelligence: IntelligenceSection = Field(default_factory=IntelligenceSection)
    context: ContextSection = Field(default_factory=ContextSection)
    confidence_weighting: ConfidenceWeightingSection = Field(default_factory=ConfidenceWeightingSection)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=DEFAULT_CONFIG_PATH)
        # env vars win over .env, which wins over YAML, which wins over hardcoded defaults.
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)

    def model_post_init(self, __context: Any) -> None:
        # cross-field overrides: a top-level env var can override a nested YAML value
        if self.log_level_override:
            self.logging.level = self.log_level_override
        if self.reasoning_model_override:
            self.reasoning.model = self.reasoning_model_override
        if self.app_env:
            self.app.env = self.app_env

    @property
    def has_anthropic_key(self) -> bool:
        return self.anthropic_api_key is not None and bool(self.anthropic_api_key.get_secret_value())

    @property
    def has_discord_token(self) -> bool:
        return self.discord_bot_token is not None and bool(self.discord_bot_token.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings instance.

    Tests that need a fresh instance should call ``get_settings.cache_clear()``.
    """
    return Settings()
