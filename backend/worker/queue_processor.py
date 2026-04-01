"""
Background queue processor for the predictive parking write path.

This module drains the Upstash Redis queue of raw parking event payloads,
resolves each to the nearest street segment via PostGIS, and bulk-inserts
them into the Supabase database.

External Dependencies: upstash-redis, sqlalchemy, geoalchemy2
State: Reads from Upstash 'parking_events_queue'; writes to 'parkingevent' table.
"""

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from upstash_redis import Redis as UpstashRedis

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import ParkingEvent, StreetSegment

logger: logging.Logger = logging.getLogger(__name__)

QUEUE_KEY = "parking_events_queue"
BATCH_SIZE = 100
SPATIAL_TOLERANCE = 0.001  # ~100 m in degrees


def _get_upstash_client() -> UpstashRedis:
    """Return a configured Upstash Redis HTTP client.

    Returns:
        UpstashRedis: Client pointed at the REST endpoint from settings.
    """
    return UpstashRedis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )


def _resolve_segment(db: Session, lat: float, lon: float) -> StreetSegment | None:
    """Find the nearest street segment within SPATIAL_TOLERANCE degrees.

    Args:
        db (Session): Active SQLAlchemy session.
        lat (float): Latitude of the event.
        lon (float): Longitude of the event.

    Returns:
        StreetSegment | None: Nearest segment, or None if none found.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    stmt = (
        select(StreetSegment)
        .filter(func.ST_DWithin(StreetSegment.geom, point, SPATIAL_TOLERANCE))
        .order_by(func.ST_Distance(StreetSegment.geom, point))
        .limit(1)
    )
    return db.scalar(stmt)


# SCA-009 — Create drain_events_queue to batch-drain Upstash queue into Supabase.
def drain_events_queue() -> None:
    """Pop up to BATCH_SIZE events from Upstash and bulk-insert into the database.

    Each raw JSON payload is resolved to a StreetSegment via PostGIS. Events
    that cannot be matched to a segment are dropped with a warning log.
    """
    upstash = _get_upstash_client()
    db: Session = SessionLocal()
    try:
        events: list[ParkingEvent] = []
        for _ in range(BATCH_SIZE):
            popped: str | list[str] | None = upstash.rpop(QUEUE_KEY)
            if popped is None:
                break
            raw = popped if isinstance(popped, str) else popped[0]
            data: dict[str, object] = json.loads(raw)
            segment = _resolve_segment(
                db, float(str(data["lat"])), float(str(data["lon"]))
            )
            if segment is None:
                logger.warning("No segment found for payload: %s", data)
                continue
            events.append(
                ParkingEvent(
                    segment_id=segment.id,
                    event_type=str(data["event_type"]),
                    user_id=str(data["user_id"]),
                )
            )
        if events:
            db.bulk_save_objects(events)
            db.commit()
            logger.info("Drained %d events from queue.", len(events))
    except Exception:
        db.rollback()
        logger.exception("Queue drain failed; transaction rolled back.")
    finally:
        db.close()


# This code satisfies SCA-009. No additional functionality added.
