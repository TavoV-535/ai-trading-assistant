"""
Startup and shutdown sequencing for the whole application.

Kept separate from the FastAPI app so it can be exercised directly in tests
(and eventually by the Discord bot entrypoint) without needing an ASGI
server running.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.state import AppState
from app.db.base import Database
from app.db.event_logger import attach_event_logger
from app.event_bus.bus import EventBus
from app.logging import configure_logging, get_logger
from app.plugins.registry import PluginRegistry
from app.reasoning.engine import ReasoningEngine
from app.reasoning.providers.claude_provider import ClaudeProvider

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def bootstrap(settings: Any | None = None, *, project_root: Path | None = None) -> AppState:
    """Bring every core system up, in dependency order, and load plugins.

    A plugin failing to initialize never aborts boot — see
    :meth:`~app.plugins.registry.PluginRegistry.load_all`. A missing AI
    provider never aborts boot either — the Reasoning Engine degrades to
    evidence-only mode (see :mod:`app.reasoning.engine`).
    """
    settings = settings or get_settings()
    root = project_root or PROJECT_ROOT

    configure_logging(settings)
    log.info("bootstrap_starting", env=settings.app.env)

    event_bus = EventBus.from_settings(settings)

    database = Database(settings)
    attach_event_logger(event_bus, database)

    provider = None
    if settings.reasoning.enabled and settings.has_anthropic_key:
        provider = ClaudeProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.reasoning.model,
        )
        log.info("reasoning_provider_ready", model=settings.reasoning.model)
    else:
        log.warning(
            "reasoning_provider_not_configured",
            detail="ANTHROPIC_API_KEY missing or reasoning.enabled=false — "
            "the Reasoning Engine will produce evidence-only summaries.",
        )

    reasoning_engine = ReasoningEngine(settings, provider=provider)
    reasoning_engine.attach(event_bus)

    plugin_registry = PluginRegistry(event_bus, settings)
    await plugin_registry.load_all(root)

    log.info(
        "bootstrap_complete",
        plugins_loaded=len(plugin_registry.plugins),
        plugins_failed=len(plugin_registry.failed),
    )

    return AppState(
        settings=settings,
        event_bus=event_bus,
        database=database,
        plugin_registry=plugin_registry,
        reasoning_engine=reasoning_engine,
        project_root=root,
    )


async def teardown(state: AppState) -> None:
    """Shut everything down in reverse dependency order."""
    log.info("teardown_starting")
    await state.plugin_registry.shutdown_all()
    await state.event_bus.shutdown()
    await state.database.dispose()
    log.info("teardown_complete")
