"""
Health check endpoint for the predictive parking service.

Provides a GET /api/v1/health endpoint that reports database connectivity,
Redis connectivity, R2 tile freshness, table row counts, and last ingestion
timestamps per source.

External Dependencies: fastapi, sqlalchemy, upstash_redis.
State: reads from database and Redis — no writes.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from upstash_redis.asyncio import Redis as AsyncRedis

from app.core.config import settings
from app.db.database import get_db
from app.db.redis_client import get_redis
from app.schemas.payload import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
)
async def get_health(
    db: Annotated[Session, Depends(get_db)],
    redis_client: Annotated[AsyncRedis, Depends(get_redis)],
) -> HealthResponse:
    """Return service health status with database and Redis connectivity.

    Always returns HTTP 200. Connectivity failures are reported as false flags
    in the response body rather than error status codes.

    Args:
        db: SQLAlchemy Session (injected).
        redis_client: Upstash Redis client (injected).

    Returns:
        HealthResponse with connectivity and data freshness information.
    """
    db_connected = _check_db(db)
    redis_connected, r2_timestamp = await _check_redis(redis_client)
    counts = _get_db_counts(db) if db_connected else {}
    ingestion = _get_last_ingestion(db) if db_connected else {}

    return HealthResponse(
        status="healthy" if db_connected and redis_connected else "degraded",
        db_connected=db_connected,
        redis_connected=redis_connected,
        r2_last_tile_upload=r2_timestamp,
        r2_public_url=settings.R2_PUBLIC_URL,
        segment_count=counts.get("segment_count", 0),
        bay_count=counts.get("bay_count", 0),
        cpz_count=counts.get("cpz_count", 0),
        enforcement_events_30d=counts.get("enforcement_events_30d", 0),
        last_ingestion=ingestion,
    )


def _check_db(db: Session) -> bool:
    """Check database connectivity with a simple SELECT 1 query.

    Args:
        db: SQLAlchemy Session.

    Returns:
        True if the database is reachable, False otherwise.
    """
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("Database connectivity check failed")
        return False


async def _check_redis(redis_client: AsyncRedis) -> tuple[bool, str | None]:
    """Check Redis connectivity and read last tile upload timestamp.

    Args:
        redis_client: Redis client (async).

    Returns:
        Tuple of (is_connected, r2_last_tile_upload timestamp or None).
    """
    try:
        ping = await redis_client.ping()
        ts = await redis_client.get("r2_last_tile_upload")
        return bool(ping), ts
    except Exception:
        logger.warning("Redis connectivity check failed")
        return False, None


def _get_db_counts(db: Session) -> dict[str, int]:
    """Query database for table row counts.

    Args:
        db: SQLAlchemy Session.

    Returns:
        Dict with segment_count, bay_count, cpz_count, enforcement_events_30d.
    """
    queries = {
        "segment_count": "SELECT COUNT(*) FROM streetsegment",
        "bay_count": "SELECT COUNT(*) FROM parking_bay",
        "cpz_count": "SELECT COUNT(*) FROM cpz_zone",
        "enforcement_events_30d": (
            "SELECT COUNT(*) FROM enforcement_event "
            "WHERE event_ts > NOW() - INTERVAL '30 days'"
        ),
    }
    return {
        key: int(db.execute(text(sql)).scalar() or 0) for key, sql in queries.items()
    }


def _get_last_ingestion(db: Session) -> dict[str, str | None]:
    """Query the most recent successful ingestion timestamp per source.

    Args:
        db: SQLAlchemy Session.

    Returns:
        Dict mapping source name to ISO timestamp of last successful run.
    """
    result = db.execute(
        text(
            "SELECT source, MAX(finished_at)::text "
            "FROM ingestion_run "
            "WHERE status = 'success' "
            "GROUP BY source"
        )
    )
    return {row[0]: row[1] for row in result.all()}


# PH6-035 — GET /api/v1/health with ingestion run tracking.
# This code satisfies PH6-035. No additional functionality added.
