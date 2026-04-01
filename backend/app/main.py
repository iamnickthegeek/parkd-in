"""
Entry point for the Predictive Parking FastAPI application.

This module initializes the FastAPI service, configures middleware,
registers API routers, and manages the lifecycle of background worker tasks.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from worker.scheduler import start_scheduler

# SCA-005 — Initialize Sentry and add an intentional error endpoint.
import sentry_sdk
from app.core.config import settings

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=1.0,
    )

# Initialize the FastAPI application.
app = FastAPI(
    title="Predictive Parking API",
    description="A heuristic-based street parking prediction service for London.",
    version="1.0.0",
)

# Configure CORS middleware with permissive cross-origin defaults.
# This allows the vanilla JS frontend to interact with the API during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register the v1 API router.
app.include_router(api_router, prefix="/api/v1")


@app.on_event("startup")
async def startup_event() -> None:
    """
    Handle application startup hooks.

    Initializes the background scheduler to start periodic prediction
    cache refreshes.
    """
    start_scheduler()


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Standard health check endpoint.

    Returns:
        dict: A status message confirming the API is operational.
    """
    return {"status": "ok", "service": "predictive_parking"}


@app.get("/sentry-debug")
async def trigger_error() -> None:
    """Trigger an intentional error to verify Sentry reporting."""
    1 / 0


# This code satisfies SCA-005. No additional functionality added.


# PKG-010 — Main Application: Initialize FastAPI app and startup hooks.
# This code satisfies PKG-010. No additional functionality added.
