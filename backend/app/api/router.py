"""
Centralized API routing for the Predictive Parking service.

This module aggregates individual feature endpoints into a single router
prefix /api/v1 for clean versioning and modularity.
"""

from fastapi import APIRouter

from app.api.endpoints import auth, parking, health

api_router = APIRouter()

# Register the auth endpoints under the /api/v1 prefix.
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Register the parking endpoints under the /api/v1 prefix.
api_router.include_router(parking.router, prefix="/parking", tags=["parking"])

# Register the health endpoint at the /api/v1 root (no sub-prefix).
api_router.include_router(health.router, tags=["health"])

# PKG-010 — Main Application: Centralize API routing.
# This code satisfies PKG-010. No additional functionality added.
