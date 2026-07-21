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

        overall_ok = db_ok and not unhealthy
        body = {
            "status": "healthy" if overall_ok else "degraded",
            "database": "healthy" if db_ok else "unhealthy",
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

    return app
