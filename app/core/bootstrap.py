"""
Startup and shutdown sequencing for the whole application.

Kept separate from the FastAPI app so it can be exercised directly in tests
(and eventually by the Discord bot entrypoint) without needing an ASGI
server running.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from app.aggregation.aggregator import EvidenceAggregator
from app.config import get_settings
from app.core.state import AppState
from app.db.base import Database
from app.db.event_logger import attach_event_logger
from app.discord.bot import TradingBot
from app.event_bus.bus import EventBus
from app.logging import configure_logging, get_logger
from app.marketdata.service import MarketDataService
from app.plugins.registry import PluginRegistry
from app.reasoning.engine import ReasoningEngine
from app.reasoning.providers.claude_provider import ClaudeProvider
from app.strategy.engine import StrategyEngine

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

    # Evidence Aggregator sits between every evidence producer (indicator
    # plugins today; news/earnings/macro/scanners later) and everything
    # that consumes evidence. It's the single interface both the Strategy
    # Engine and the Reasoning Engine subscribe to — neither subscribes to
    # raw EvidenceProduced directly. See app/aggregation/aggregator.py.
    evidence_aggregator = EvidenceAggregator(settings)
    evidence_aggregator.attach(event_bus)

    strategy_engine = StrategyEngine(settings)
    strategy_engine.load(root)
    strategy_engine.attach(event_bus)

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

    # Command plugins (e.g. /analyze) may need to read the *current*
    # evidence/reasoning state synchronously, not just react to events — see
    # PluginContext's docstring for the scope of this exception.
    plugin_registry = PluginRegistry(
        event_bus,
        settings,
        reasoning_engine=reasoning_engine,
        evidence_aggregator=evidence_aggregator,
        strategy_engine=strategy_engine,
    )

    # Phase 1: market data provider plugins load first, in isolation. The
    # Market Data Abstraction Layer can't exist until concrete provider
    # instances do, and the Scanner Engine (phase 2) needs the abstraction
    # layer, never a specific provider — see app/marketdata/service.py.
    await plugin_registry.load_all(root, search_paths=["plugins/market_data"])
    market_data_service = MarketDataService(settings, plugin_registry)
    plugin_registry.set_market_data_service(market_data_service)

    # Phase 2: everything else -- indicators, commands, scanners, ...
    remaining_paths = [p for p in settings.plugins.search_paths if p != "plugins/market_data"]
    await plugin_registry.load_all(root, search_paths=remaining_paths)

    discord_bot: TradingBot | None = None
    discord_task: "asyncio.Task[None] | None" = None
    if settings.has_discord_token:
        discord_bot = TradingBot(settings, event_bus, plugin_registry)
        token = settings.discord_bot_token.get_secret_value()
        discord_task = asyncio.create_task(discord_bot.start(token))
        log.info("discord_bot_starting")
    else:
        log.warning(
            "discord_bot_not_configured",
            detail="DISCORD_BOT_TOKEN missing — Discord commands will not be reachable "
            "until it's set. See docs/DISCORD_BOT_SETUP.md.",
        )

    log.info(
        "bootstrap_complete",
        plugins_loaded=len(plugin_registry.plugins),
        plugins_failed=len(plugin_registry.failed),
        strategies_loaded=len(strategy_engine.strategies),
        market_data_providers=[p.provider_name for p in market_data_service.providers],
        discord_enabled=discord_bot is not None,
    )

    return AppState(
        settings=settings,
        event_bus=event_bus,
        database=database,
        plugin_registry=plugin_registry,
        evidence_aggregator=evidence_aggregator,
        strategy_engine=strategy_engine,
        reasoning_engine=reasoning_engine,
        market_data_service=market_data_service,
        project_root=root,
        discord_bot=discord_bot,
        discord_task=discord_task,
    )


async def teardown(state: AppState) -> None:
    """Shut everything down in reverse dependency order."""
    log.info("teardown_starting")
    if state.discord_bot is not None:
        await state.discord_bot.close()
    if state.discord_task is not None:
        state.discord_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.discord_task
    await state.plugin_registry.shutdown_all()
    await state.event_bus.shutdown()
    await state.database.dispose()
    log.info("teardown_complete")
