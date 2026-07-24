"""
FastAPI application factory.

Startup/shutdown is wired through the ASGI lifespan, which is how graceful
shutdown works: uvicorn intercepts SIGINT/SIGTERM, runs the lifespan's
shutdown phase (tearing down plugins, the event bus, and the database
cleanly), and only then exits. Docker Compose's ``restart: unless-stopped``
handles bringing the process back up after a crash.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.bootstrap import bootstrap, teardown
from app.core.state import AppState
from app.logging import get_logger
from app.scanner.plugin import ScannerPlugin

log = get_logger(__name__)


def create_app(settings: Any | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = await bootstrap(settings)
        app.state.core = state
        try:
            yield
        finally:
            await teardown(state)

    app = FastAPI(
        title=settings.app.name,
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        state: AppState = app.state.core
        db_ok = await state.database.health()
        plugin_health = await state.plugin_registry.health_check_all()
        unhealthy = [name for name, h in plugin_health.items() if h.status == "unhealthy"]

        if state.discord_bot is None:
            discord_status = "not_configured"
        elif state.discord_bot.is_ready():
            discord_status = "connected"
        else:
            discord_status = "connecting"

        overall_ok = db_ok and not unhealthy
        body = {
            "status": "healthy" if overall_ok else "degraded",
            "database": "healthy" if db_ok else "unhealthy",
            "discord": discord_status,
            "plugins": {name: h.status for name, h in plugin_health.items()},
            "plugins_failed_to_load": list(state.plugin_registry.failed.keys()),
        }
        return JSONResponse(body, status_code=200 if overall_ok else 503)

    @app.get("/plugins")
    async def plugins() -> dict:
        state: AppState = app.state.core
        return {
            "loaded": {
                name: plugin.metadata().model_dump() for name, plugin in state.plugin_registry.plugins.items()
            },
            "failed": state.plugin_registry.failed,
        }

    @app.get("/strategies")
    async def strategies() -> dict:
        state: AppState = app.state.core
        return {
            "loaded": [
                {
                    "name": s.name,
                    "required": sorted(s.required),
                    "optional": sorted(s.optional),
                    "minimum_score": s.minimum_score,
                    "repeat_policy": s.repeat_policy,
                }
                for s in state.strategy_engine.strategies
            ]
        }

    @app.get("/watchlist")
    async def watchlist() -> dict:
        state: AppState = app.state.core
        profiles = state.portfolio_engine.ranked_watchlist()
        return {
            "watchlist": list(state.portfolio_engine.watchlist),
            "ranked": [p.model_dump(mode="json") for p in profiles],
        }

    @app.get("/scanners")
    async def scanners() -> dict:
        state: AppState = app.state.core
        scanner_plugins = [p for p in state.plugin_registry.plugins.values() if isinstance(p, ScannerPlugin)]
        return {
            "market_data_providers": [p.provider_name for p in state.market_data_service.providers],
            "loaded": [
                {
                    "name": s.name,
                    "watchlist": list(s.watchlist),
                    "timeframes": list(s.timeframes),
                    "interval_seconds": s.interval_seconds,
                    "asset_class": s.asset_class,
                    "health": (await s.health()).model_dump(mode="json"),
                }
                for s in sorted(scanner_plugins, key=lambda s: s.name)
            ],
        }

    return app
