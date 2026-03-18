from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import (
    configure_runtime_state,
    dispatcher_enabled,
    load_runtime_state,
    router as api_router,
    start_task_dispatcher,
    stop_task_dispatcher,
)
from src.sandbox import cleanup_shared_sandbox_managers

logger = structlog.get_logger()


def _configure_structlog() -> None:
    """Set up structlog with JSON rendering for production, console for dev."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_structlog()
    configure_runtime_state(
        state_path=os.environ.get("MINIONS_API_STATE_PATH"),
        metrics_path=os.environ.get("MINIONS_METRICS_PATH"),
    )
    load_runtime_state()
    await start_task_dispatcher()
    logger.info("minion_server_starting", dispatcher_enabled=dispatcher_enabled())
    yield
    await stop_task_dispatcher()
    await cleanup_shared_sandbox_managers()
    logger.info("minion_server_stopping")


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    app = FastAPI(
        title="Minions API",
        description="Autonomous coding agent orchestration server",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # CORS — permissive in development, tighten for production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    # Optionally mount the monitoring dashboard
    try:
        from src.monitoring.dashboard import dashboard_app

        app.mount("/dashboard", dashboard_app)
    except Exception:
        logger.debug("dashboard_not_loaded")

    return app


app = create_app()


def start(host: str = "0.0.0.0", port: int = 8000, role: str | None = None) -> None:
    """Entry point for programmatic server startup."""
    _configure_structlog()
    if role is not None:
        os.environ["MINIONS_SERVER_ROLE"] = role
    uvicorn.run(
        "src.api.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
