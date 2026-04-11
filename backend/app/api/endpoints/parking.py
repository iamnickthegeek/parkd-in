"""
API endpoints for parking probability and event intake.

Provides read endpoints for parking probability (Redis-backed) and
write endpoints for submitting parking events (queue-backed).

External Dependencies:
- fastapi
- upstash_redis
- math (haversine calculation)
State: reads from segment_probs Redis hash, writes to parking_events_queue.
"""

import json
import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from upstash_redis import Redis as UpstashRedis

from app.api.deps import check_spatial_rate_limit, verify_token
from app.db.database import get_db
from app.core.config import settings
from app.core.engine import WINDOW_MULTIPLIERS, calculate_probability_windowed
from app.db.models import StreetSegment
from app.schemas.payload import (
    BestNearbyRecommendation,
    BestNearbyResponse,
    ParkingEventCreate,
    ProbabilitySegment,
    SegmentDetailResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

QUEUE_KEY = "parking_events_queue"
EARTH_RADIUS_M = 6371000


def _get_upstash_client() -> UpstashRedis:
    """Return a configured Upstash Redis HTTP client.

    Returns:
        UpstashRedis: Client pointed at the REST endpoint from settings.
    """
    return UpstashRedis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate haversine distance between two points in metres.

    Args:
        lat1: Latitude of point 1 in degrees.
        lon1: Longitude of point 1 in degrees.
        lat2: Latitude of point 2 in degrees.
        lon2: Longitude of point 2 in degrees.

    Returns:
        float: Distance in metres.
    """
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _get_cached_segments(redis_client: UpstashRedis) -> dict[str, dict[str, Any]]:
    """Load and parse all segments from the Redis segment_probs hash.

    Args:
        redis_client: Upstash Redis client.

    Returns:
        Dict mapping segment_id to parsed segment data dict.
    """
    raw = redis_client.hgetall("segment_probs") or {}
    return {k: json.loads(v) for k, v in raw.items()}


# SCA-008 — Update create_parking_event to enqueue payload on Upstash and return 202.
@router.post("/event", status_code=status.HTTP_202_ACCEPTED)
async def create_parking_event(
    payload: ParkingEventCreate,
    user_id: str = Depends(verify_token),
) -> dict[str, str]:
    """
    Enqueue a parking event on Upstash Redis for async processing.

    Args:
        payload (ParkingEventCreate): User report data (lat, lon, event_type).
        user_id (str): Verified user ID from JWT.

    Returns:
        dict: Acknowledgement that the event was accepted for processing.
    """
    event_data = payload.model_dump()
    event_data["user_id"] = user_id
    json_payload = json.dumps(event_data)

    upstash = _get_upstash_client()
    upstash.lpush(QUEUE_KEY, json_payload)

    return {"status": "accepted"}


# PH6-032 — GET /api/v1/parking/probability reads from Redis cache.
@router.get("/probability", response_model=list[ProbabilitySegment])
async def get_parking_probability(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: int = Query(default=200, ge=1, le=500),
    window_min: int = Query(default=10),
    _rate_limit: Annotated[None, Depends(check_spatial_rate_limit)] = None,
) -> list[ProbabilitySegment]:
    """Return parking probability for segments near the given location.

    Reads from the segment_probs Redis cache — zero PostGIS queries.

    Args:
        lat: Query latitude.
        lon: Query longitude.
        radius: Search radius in metres (max 500).
        window_min: Time window in minutes (must be 5, 10, or 15).
        redis_client: Redis client (injected).
        _rate_limit: Rate limit dependency (injected).

    Returns:
        List of ProbabilitySegment sorted by adjusted probability descending.

    Raises:
        HTTPException: 400 if window_min is not 5, 10, or 15.
    """
    if window_min not in WINDOW_MULTIPLIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="window_min must be 5, 10, or 15",
        )

    upstash = _get_upstash_client()
    segments = _get_cached_segments(upstash)
    multiplier = WINDOW_MULTIPLIERS[window_min]
    last_updated = upstash.get("r2_last_tile_upload")

    results: list[ProbabilitySegment] = []
    for sid, seg in segments.items():
        dist = _haversine_m(lat, lon, seg["lat"], seg["lon"])
        if dist <= radius:
            adj_prob = min(1.0, seg["prob"] * multiplier)
            results.append(
                ProbabilitySegment(
                    segment_id=sid,
                    street_name=seg.get("street_name", ""),
                    probability=adj_prob,
                    window_minutes=window_min,
                    bay_count=seg.get("bay_count", 0),
                    distance_metres=round(dist, 1),
                    last_updated=last_updated,
                )
            )

    results.sort(key=lambda r: r.probability, reverse=True)
    return results


# PH6-033 — GET /api/v1/parking/best_nearby reads from Redis cache.
@router.get("/best_nearby", response_model=BestNearbyResponse)
async def get_best_nearby(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: int = Query(
        default=500, ge=50, le=2000, description="Search radius in metres"
    ),
    limit: int = Query(default=5, ge=1, le=10),
    window_min: int = Query(
        default=10, description="Time window: 5, 10, or 15 minutes"
    ),
    _rate_limit: Annotated[None, Depends(check_spatial_rate_limit)] = None,
) -> BestNearbyResponse:
    """Return top-ranked parking recommendations near a location.

    Filters segments to those within `radius` metres, applies the
    `window_min` probability multiplier, then scores each candidate as
    a weighted combination of probability (70%) and proximity (30%).
    Returns the top `limit` results, ranked by score descending.

    Reads from the segment_probs Redis cache — zero PostGIS queries.

    Args:
        lat: Query latitude (destination or current GPS position).
        lon: Query longitude.
        radius: Search radius in metres (default 500, max 2000).
        limit: Maximum recommendations to return (default 5, max 10).
        window_min: Prediction horizon — must be 5, 10, or 15.
        _rate_limit: Rate limit dependency (injected).

    Returns:
        BestNearbyResponse with proximity-scored recommendations.

    Raises:
        HTTPException: 400 if window_min is not 5, 10, or 15.
    """
    if window_min not in WINDOW_MULTIPLIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="window_min must be 5, 10, or 15",
        )

    upstash = _get_upstash_client()
    segments = _get_cached_segments(upstash)
    multiplier = WINDOW_MULTIPLIERS[window_min]

    # Score = 70% adjusted probability + 30% proximity weight.
    # Proximity weight = 1.0 at the query point, 0.0 at the radius edge.
    scored: list[tuple[float, float, float, str, dict[str, Any]]] = []
    for sid, seg in segments.items():
        dist = _haversine_m(lat, lon, seg["lat"], seg["lon"])
        if dist > radius:
            continue
        adj_prob = min(1.0, seg["prob"] * multiplier)
        proximity = 1.0 - (dist / radius)
        score = adj_prob * 0.70 + proximity * 0.30
        scored.append((score, adj_prob, dist, sid, seg))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    recommendations = [
        BestNearbyRecommendation(
            rank=i + 1,
            segment_id=sid,
            street_name=seg.get("street_name", ""),
            probability=adj_prob,
            walk_metres=round(dist, 1),
            lat=seg["lat"],
            lon=seg["lon"],
            restriction_active=False,
        )
        for i, (_, adj_prob, dist, sid, seg) in enumerate(top)
    ]

    return BestNearbyResponse(
        recommendations=recommendations,
        generated_at=datetime.now(UTC).isoformat(),
    )


def _get_cpz_info(db: Session, segment_id: uuid.UUID) -> tuple[bool, Any]:
    """Query CPZ zone restriction data for a segment.

    Args:
        db: SQLAlchemy Session.
        segment_id: Segment UUID.

    Returns:
        Tuple of (restriction_active, restriction_schedule).
    """
    cpz_result = db.execute(
        text(
            "SELECT restriction_schedule FROM cpz_zone c "
            "WHERE ST_Within((SELECT ST_Centroid(geom) FROM streetsegment WHERE id=:sid), c.geom) "
            "LIMIT 1"
        ),
        {"sid": str(segment_id)},
    ).scalar()
    return cpz_result is not None, cpz_result


def _get_crowd_event(
    db: Session, segment_id: uuid.UUID
) -> tuple[str | None, float | None]:
    """Query the most recent crowd event for a segment.

    Args:
        db: SQLAlchemy Session.
        segment_id: Segment UUID.

    Returns:
        Tuple of (event_type, age_in_minutes) or (None, None).
    """
    crowd_result = db.execute(
        text(
            "SELECT event_type, timestamp FROM parkingevent "
            "WHERE segment_id=:sid ORDER BY timestamp DESC LIMIT 1"
        ),
        {"sid": str(segment_id)},
    ).first()
    if not crowd_result:
        return None, None
    age = datetime.now(UTC) - crowd_result[1]
    return crowd_result[0], round(age.total_seconds() / 60, 1)


def _get_traffic_speed(db: Session, segment_id: uuid.UUID) -> float | None:
    """Query recent traffic speed for a segment.

    Args:
        db: SQLAlchemy Session.
        segment_id: Segment UUID.

    Returns:
        Traffic speed in km/h or None.
    """
    return db.execute(
        text(
            "SELECT speed FROM traffichistory "
            "WHERE segment_id=:sid AND timestamp > NOW() - INTERVAL '30 minutes' "
            "ORDER BY timestamp DESC LIMIT 1"
        ),
        {"sid": str(segment_id)},
    ).scalar()


# PH6-034 — GET /api/v1/parking/segment/{segment_id} with live DB detail.
@router.get("/segment/{segment_id}", response_model=SegmentDetailResponse)
async def get_segment_detail(
    segment_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> SegmentDetailResponse:
    """Return full detail for a single street segment.

    Queries the live database for segment, bay, CPZ, crowd event, and
    traffic data. Uses calculate_probability_windowed for probabilities.

    Args:
        segment_id: UUID of the street segment.
        db: SQLAlchemy Session (injected).

    Returns:
        SegmentDetailResponse with full segment information.

    Raises:
        HTTPException: 404 if segment not found.
    """
    segment = db.get(StreetSegment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")

    bay_count = len(segment.bays) if segment.bays else 0
    restr_active, restr_schedule = _get_cpz_info(db, segment_id)
    crowd_type, crowd_age = _get_crowd_event(db, segment_id)
    traffic_spd = _get_traffic_speed(db, segment_id)
    windowed = calculate_probability_windowed(segment, datetime.now(UTC), [], db)

    return SegmentDetailResponse(
        segment_id=str(segment_id),
        street_name=segment.street_name or "",
        probability_5min=windowed[5],
        probability_10min=windowed[10],
        probability_15min=windowed[15],
        bay_count=bay_count,
        restriction_active=restr_active,
        restriction_schedule=restr_schedule,
        last_crowd_event=crowd_type,
        last_crowd_event_age_min=crowd_age,
        traffic_speed=traffic_spd,
    )


# This code satisfies SCA-008. No additional functionality added.
# SCA-014 — DELETE GET /map endpoint; API no longer serves reads (Phase 4).
# This code satisfies SCA-014. No additional functionality added.
# PH6-032 — GET /api/v1/parking/probability reads from Redis cache.
# This code satisfies PH6-032. No additional functionality added.
# PH6-033 — GET /api/v1/parking/best_nearby reads from Redis cache.
# This code satisfies PH6-033. No additional functionality added.
# PH6-034 — GET /api/v1/parking/segment/{segment_id} with live DB detail.
# This code satisfies PH6-034. No additional functionality added.
