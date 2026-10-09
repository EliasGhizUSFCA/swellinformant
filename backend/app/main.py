"""FastAPI application entry point: ``uvicorn app.main:app``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, notifications, opportunities, searches, spots, system, users, webhooks
from app.core.config import get_settings
from app.core.errors import install_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import CSRFMiddleware, SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    logger.info(
        "Starting %s (env=%s, forecasts=%s, flights=%s, email=%s, sms=%s)",
        settings.app_name,
        settings.app_env,
        settings.forecast_provider_list,
        settings.flight_provider,
        settings.email_provider,
        settings.sms_provider,
    )
    missing = settings.missing_live_credentials()
    if missing:
        logger.warning("Missing credentials for configured live providers: %s", ", ".join(missing))
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Swell Travel Agent API",
        version="1.0.0",
        description=(
            "Surf-forecast-driven travel discovery. All state-changing requests need the "
            "session cookie and an `X-CSRF-Token` header matching the `sta_csrf` cookie "
            "(GET `/api/auth/csrf` first)."
        ),
        lifespan=lifespan,
    )
    install_exception_handlers(app)
    # Order: the last added middleware runs first.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    for module in (system, auth, users, spots, searches, opportunities, notifications, webhooks):
        app.include_router(module.router)
    if settings.enable_dev_endpoints:
        from app.api import dev

        app.include_router(dev.router)
    return app


app = create_app()
