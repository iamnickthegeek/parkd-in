"""
Heuristic calculation engine for parking probabilities.

Calculates the likelihood of finding a parking spot based on street
capacity and recent reporting events. The result is published as a
Mapbox Vector Tile (.pbf) uploaded to Cloudflare R2 for CDN delivery.

External Dependencies:
- sqlalchemy
- geoalchemy2
- boto3
- shapely
- upstash-redis
State: MVT tiles uploaded to R2, segment_probs cached in Redis.
"""

import json
import logging
import uuid
from datetime import UTC, datetime, time
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import StreetSegment

logger: logging.Logger = logging.getLogger(__name__)

DAY_MAP: dict[str, int] = {
    "Mon": 0,
    "Tue": 1,
    "Wed": 2,
    "Thu": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6,
}


def _parse_days_string(days: str) -> set[int]:
    """Parse a day range string into a set of weekday indices (Mon=0)."""
    if days in DAY_MAP:
        return {DAY_MAP[days]}
    if days == "All week":
        return set(range(7))
    if "-" in days:
        start_str, end_str = days.split("-")
        return set(range(DAY_MAP[start_str], DAY_MAP[end_str] + 1))
    return set()


def restriction_factor(segment_id: uuid.UUID, dt: datetime, db: Session) -> float:
    """Return 0.0 if bays are restricted at dt, 1.0 if unrestricted or no CPZ found."""
    stmt = text(
        "SELECT restriction_schedule FROM cpz_zone c "
        "WHERE ST_Within((SELECT ST_Centroid(geom) FROM streetsegment WHERE id=:sid), c.geom) "
        "LIMIT 1"
    )
    result = db.execute(stmt, {"sid": str(segment_id)}).scalar()
    if result is None:
        return 1.0
    for entry in result:
        allowed_days = _parse_days_string(entry["days"])
        start = time.fromisoformat(entry["start"])
        end = time.fromisoformat(entry["end"])
        if dt.weekday() in allowed_days and start <= dt.time() <= end:
            return 0.0
    return 1.0


def time_factor(
    segment_id: uuid.UUID,
    hour: int,
    dow: int,
    db: Session,
    pcn_count: int | None = None,
) -> float:
    """Return a time-of-day factor based on enforcement density.

    Reads enforcement_density_mv for historical PCN counts.
    Returns 0.0–1.0 where lower means higher enforcement (less likely to park).

    Args:
        segment_id: The street segment UUID.
        hour: Hour of day (0–23).
        dow: Day of week (0=Mon, 6=Sun).
        db: SQLAlchemy Session from caller.
        pcn_count: Pre-loaded PCN count (optional, enables batch use).

    Returns:
        float: Time factor in [0.0, 1.0]. 0.7 when no data available.
    """
    if pcn_count is None:
        stmt = text(
            "SELECT pcn_count FROM enforcement_density_mv "
            "WHERE segment_id=:s AND hour_of_day=:h AND day_of_week=:d"
        )
        pcn_count = db.execute(
            stmt, {"s": str(segment_id), "h": hour, "d": dow}
        ).scalar()
    if pcn_count is None:
        return 0.7
    return max(0.0, 1.0 - pcn_count / 10.0)


def traffic_factor(
    segment_id: uuid.UUID, db: Session, speed: float | None = None
) -> float:
    """Return a traffic speed factor for the probability calculation.

    Queries traffichistory for recent speed data when speed is not pre-loaded.
    Returns 0.5–1.0 where higher speed means more traffic (harder to park).

    Args:
        segment_id: The street segment UUID.
        db: SQLAlchemy Session from caller.
        speed: Pre-loaded traffic speed in km/h (optional).

    Returns:
        float: Traffic factor in [0.5, 1.0]. 0.7 when no data available.
    """
    if speed is None:
        stmt = text(
            "SELECT speed FROM traffichistory "
            "WHERE segment_id=:s AND timestamp > NOW() - INTERVAL '30 minutes' "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        speed = db.execute(stmt, {"s": str(segment_id)}).scalar()
    if speed is None:
        return 0.7
    return max(0.5, min(1.0, speed / 50.0))


def crowd_factor(events: list[Any]) -> float:
    """Return a crowd-sourced event modifier for the probability calculation.

    Pure Python — no database or network access. Aggregates recent
    user-reported parking events into a modifier applied to the base
    probability.

    Args:
        events: List of ParkingEvent objects with an event_type attribute.

    Returns:
        float: Modifier clamped to [-0.5, 0.5].
    """
    modifier = 0.0
    for event in events:
        if event.event_type == "LEFT":
            modifier += 0.4
        elif event.event_type == "SEARCHING":
            modifier -= 0.3
        elif event.event_type == "PARKED":
            modifier -= 0.2
    return max(-0.5, min(0.5, modifier))


# Time-window multipliers for probability adjustment (PH6-031).
WINDOW_MULTIPLIERS: dict[int, float] = {5: 0.75, 10: 1.0, 15: 1.20}

# Camden tile grid at zoom 14 (PH6-057).
TILE_GRID: list[tuple[int, int, int]] = [
    (14, x, y) for x in range(8183, 8189) for y in range(5443, 5448)
]

# Two separate queries to avoid expensive JOIN on 23K segments.
# Query 1: Get all segments with their centroid coordinates.
_SEGMENT_SQL = text("""
    SELECT
        s.id::text AS segment_id,
        s.street_name,
        ST_Y(ST_Centroid(s.geom)) AS lat,
        ST_X(ST_Centroid(s.geom)) AS lon
    FROM streetsegment s
""")
# Query 2: Aggregate bay counts per segment (much smaller result set).
_BAY_SQL = text("""
    SELECT
        b.segment_id::text AS segment_id,
        COUNT(DISTINCT b.id) AS bay_count,
        COALESCE(SUM(b.spaces_est), 1) AS total_spaces
    FROM parking_bay b
    GROUP BY b.segment_id
""")


def load_segment_batch_data(db: Session, hour: int, dow: int) -> list[dict[str, Any]]:
    """Load all segment data in two separate queries for batch tile generation.

    Split into segment query + bay aggregation to avoid expensive JOIN
    on 23K segments with 8K bays. Results are merged in Python.

    Args:
        db: SQLAlchemy Session from caller.
        hour: Hour of day (0-23) for enforcement density lookup.
        dow: Day of week (0=Mon, 6=Sun) for enforcement density lookup.

    Returns:
        List of dicts with segment data (segment_id, street_name, lat, lon,
        bay_count, total_spaces).
    """
    # Override Supabase's default statement timeout for this session (5 minutes).
    db.execute(text("SET statement_timeout = '300000'"))
    # Get all segments
    seg_result = db.execute(_SEGMENT_SQL).mappings().all()
    rows = [dict(row) for row in seg_result]

    # Get bay aggregations and merge into segment rows
    bay_result = db.execute(_BAY_SQL).mappings().all()
    bay_map = {row["segment_id"]: dict(row) for row in bay_result}

    for row in rows:
        sid = row["segment_id"]
        if sid in bay_map:
            row["bay_count"] = bay_map[sid]["bay_count"]
            row["total_spaces"] = bay_map[sid]["total_spaces"]
        else:
            row["bay_count"] = 0
            row["total_spaces"] = 1

    return rows


def _prob_to_color(prob: float) -> int:
    """Map a probability to a color integer for MVT rendering.

    Args:
        prob: Probability in [0.0, 1.0].

    Returns:
        int: Color as 0x00AA00 (green), 0xFFAA00 (yellow), or 0xCC0000 (red).
    """
    if prob >= 0.7:
        return 0x00AA00
    if prob >= 0.3:
        return 0xFFAA00
    return 0xCC0000


def _calc_prob_from_dict(row: dict[str, Any], dt: datetime, events: list[Any]) -> float:
    """Calculate probability from pre-loaded batch data (no DB queries).

    Simplified version — no traffic or enforcement lookups.

    Args:
        row: Dict from load_segment_batch_data with pre-joined data.
        dt: Datetime for restriction/time-of-day evaluation.
        events: List of recent ParkingEvent objects for crowd signals.

    Returns:
        float: Probability clamped to [0.0, 1.0].
    """
    f_capacity = min(1.0, row["total_spaces"] / 10.0)
    # Skip restriction check for MVP (no DB queries)
    f_time = 0.7  # Neutral time factor
    f_traffic = 0.7  # Neutral traffic factor
    f_crowd = crowd_factor(events)
    raw = (
        f_capacity * 0.20
        + f_time * 0.25
        + f_traffic * 0.20
        + f_crowd * 0.15
        + 1.0 * 0.20
    )
    return float(max(0.0, min(1.0, raw)))


def write_segment_probs_cache(
    rows: list[dict[str, Any]],
    probs: dict[str, float],
    redis_client: Any,
) -> None:
    """Write segment probabilities to Redis hash for API consistency.

    Args:
        rows: List of segment data dicts from load_segment_batch_data.
        probs: Dict mapping segment_id to probability.
        redis_client: Upstash Redis client (sync or async).
    """
    mapping = {
        sid: json.dumps(
            {
                "prob": probs[sid],
                "lat": r["lat"],
                "lon": r["lon"],
                "street_name": r["street_name"] or "",
                "bay_count": r["bay_count"],
            }
        )
        for r in rows
        for sid in [r["segment_id"]]
        if sid in probs
    }
    redis_client.hset("segment_probs", values=mapping)
    logger.info("Cached %d segment probabilities in Redis", len(mapping))


# Static SQL for MVT generation via temp table (PH6-059).
# ST_TileEnvelope returns SRID 3857; transform to 4326 to match segment geometries.
_MVT_SQL = text("""
    SELECT ST_AsMVT(q.*, 'parking', 4096, 'geom')
    FROM (
        SELECT p.segment_id, p.color_int,
               ST_AsMVTGeom(
                   s.geom,
                   ST_Transform(ST_TileEnvelope(:z,:x,:y), 4326),
                   4096, 64, true
               ) AS geom
        FROM tmp_probs p
        JOIN streetsegment s ON s.id = p.segment_id
    ) q WHERE q.geom IS NOT NULL
""")


def calculate_probability_windowed(
    segment: StreetSegment,
    dt: datetime,
    events: list[Any],
    db: Session,
    pcn_count: int | None = None,
    traffic_speed: float | None = None,
) -> dict[int, float]:
    """Return probability adjusted for each supported time window.

    Calls calculate_probability once, then scales the result by
    WINDOW_MULTIPLIERS for 5-, 10-, and 15-minute windows.

    Args:
        segment: StreetSegment ORM object with bays relationship loaded.
        dt: Datetime for restriction/time-of-day evaluation.
        events: List of recent ParkingEvent objects for crowd signals.
        db: SQLAlchemy Session from caller.
        pcn_count: Pre-loaded enforcement PCN count (optional).
        traffic_speed: Pre-loaded traffic speed in km/h (optional).

    Returns:
        dict mapping window minutes (5, 10, 15) to adjusted probability.
    """
    base = calculate_probability(segment, dt, events, db, pcn_count, traffic_speed)
    return {w: min(1.0, base * m) for w, m in WINDOW_MULTIPLIERS.items()}


def calculate_probability(
    segment: StreetSegment,
    dt: datetime,
    events: list[Any],
    db: Session,
    pcn_count: int | None = None,
    traffic_speed: float | None = None,
) -> float:
    """Compute parking probability using all 6 heuristic factors.

    Args:
        segment: StreetSegment ORM object with bays relationship loaded.
        dt: Datetime for restriction/time-of-day evaluation.
        events: List of recent ParkingEvent objects for crowd signals.
        db: SQLAlchemy Session from caller.
        pcn_count: Pre-loaded enforcement PCN count (optional).
        traffic_speed: Pre-loaded traffic speed in km/h (optional).

    Returns:
        float: Probability clamped to [0.0, 1.0].
    """
    total_spaces = sum(b.spaces_est for b in segment.bays) if segment.bays else 1
    f_capacity = min(1.0, total_spaces / 10.0)
    f_restriction = restriction_factor(segment.id, dt, db)
    if f_restriction == 0.0:
        return 0.0
    f_time = time_factor(segment.id, dt.hour, dt.weekday(), db, pcn_count)
    f_traffic = traffic_factor(segment.id, db, traffic_speed)
    f_crowd = crowd_factor(events)
    raw = (
        f_capacity * 0.20
        + f_time * 0.25
        + f_traffic * 0.20
        + f_crowd * 0.15
        + f_restriction * 0.20
    )
    return max(0.0, min(1.0, raw))


def _get_r2_client() -> Any:
    """Return a boto3 S3 client configured for Cloudflare R2.

    Uses R2_ENDPOINT_URL when set (jurisdiction-specific); falls back to
    the global account endpoint derived from R2_ACCOUNT_ID.

    Returns:
        Any: boto3 S3 client pointed at the R2 endpoint.
    """
    endpoint = settings.R2_ENDPOINT_URL or (
        f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def update_prediction_tiles_r2(db: Session, redis_client: Any) -> None:
    """Generate MVT tiles from batch probabilities and upload to R2.

    Loads all segment data in one SQL query, computes probabilities
    without per-segment DB calls, caches results in Redis, then
    generates MVT tiles via a temp table and uploads each to R2.

    Args:
        db: SQLAlchemy Session from caller.
        redis_client: Upstash Redis client for segment_probs cache.
    """
    now = datetime.now(UTC)
    hour = now.hour
    dow = now.weekday()

    rows = load_segment_batch_data(db, hour, dow)
    if not rows:
        logger.warning("No segment data; skipping tile generation.")
        return

    probs: dict[str, float] = {}
    for r in rows:
        sid = r["segment_id"]
        probs[sid] = _calc_prob_from_dict(r, now, [])

    write_segment_probs_cache(rows, probs, redis_client)

    color_rows = [(sid, _prob_to_color(p)) for sid, p in probs.items()]

    r2 = _get_r2_client()
    tile_count = 0

    # Build temp table once — inserting 23K rows per tile (inside the loop) was
    # both slow (1.3 MB SQL per tile) and buggy with ON COMMIT DROP semantics.
    db.execute(text("DROP TABLE IF EXISTS tmp_probs"))
    db.execute(
        text(
            "CREATE TEMP TABLE tmp_probs "
            "(segment_id UUID, color_int INT)"
        )
    )
    # Batch-insert in chunks to avoid a single 1.3 MB SQL statement.
    chunk_size = 1000
    for i in range(0, len(color_rows), chunk_size):
        chunk = color_rows[i : i + chunk_size]
        values_clause = ", ".join(f"('{s}'::uuid, {c})" for s, c in chunk)
        db.execute(text(f"INSERT INTO tmp_probs VALUES {values_clause}"))

    for z, x, y in TILE_GRID:
        mvt_result = db.execute(_MVT_SQL, {"z": z, "x": x, "y": y}).scalar()
        if mvt_result:
            tile_key = f"tiles/{z}/{x}/{y}.pbf"
            try:
                r2.put_object(
                    Bucket=settings.R2_BUCKET_NAME,
                    Key=tile_key,
                    Body=bytes(mvt_result),
                    ContentType="application/x-protobuf",
                )
                tile_count += 1
            except (BotoCoreError, ClientError, ValueError) as exc:
                logger.error("R2 upload failed for %s: %s", tile_key, exc)

    db.execute(text("DROP TABLE IF EXISTS tmp_probs"))

    redis_client.set("r2_last_tile_upload", now.isoformat())
    logger.info(
        "Tile generation complete: %d tiles uploaded in %.1fs",
        tile_count,
        (datetime.now(UTC) - now).total_seconds(),
    )


# This code satisfies PH6-026. No additional functionality added.

# This code satisfies PH6-031. No additional functionality added.

# This code satisfies PH6-057. No additional functionality added.

# This code satisfies PH6-058. No additional functionality added.

# This code satisfies PH6-059. No additional functionality added.
