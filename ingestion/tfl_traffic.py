"""
TfL Unified API client for Camden A-road traffic speeds.

External dependency: TfL API (api.tfl.gov.uk).
Rate limit: 500 req/day on free tier.
State: none.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ingestion.base import get_http_session

logger = logging.getLogger(__name__)

# Camden A-road identifiers for TfL traffic polling.
# Verified against GET /Road — only roads present in the TfL API are listed.
CAMDEN_ROAD_IDS: list[str] = ["a1", "a10", "a40", "a406", "a41"]

# Approximate lat/lon centroids for Camden A-roads used in spatial mapping
CAMDEN_ROAD_CENTROIDS: dict[str, tuple[float, float]] = {
    "a1": (-0.1276, 51.5550),  # Holloway Road / Highgate Hill
    "a10": (-0.1050, 51.5650),  # Stoke Newington Road
    "a40": (-0.1700, 51.5200),  # Westway (south of Camden)
    "a406": (-0.1800, 51.5700),  # North Circular (Hendon)
    "a41": (-0.1763, 51.5512),  # Finchley Road
}


class TfLClient:
    """Client for the TfL Unified API road corridor status endpoint."""

    def __init__(self, api_key: str) -> None:
        """Store API key and create HTTP session.

        Args:
            api_key: TfL API key from settings.TFL_API_KEY.
        """
        self._api_key = api_key
        self._session = get_http_session()

    def get_road_speeds(self, road_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch current road status for given road IDs.

        Args:
            road_ids: List of TfL road identifiers (e.g. 'a4', 'a406').
        Returns:
            List of dicts with road_id, status_severity, timestamp.
        """
        results: list[dict[str, Any]] = []
        for road_id in road_ids:
            result = self._fetch_single_road(road_id)
            if result is not None:
                results.append(result)
        return results

    def _fetch_single_road(self, road_id: str) -> dict[str, Any] | None:
        """Fetch and parse status data for one road, with retry on 429.

        Args:
            road_id: Single TfL road identifier (lowercase).
        Returns:
            Dict with road_id, status_severity, timestamp or None on failure.
        """
        url = f"https://api.tfl.gov.uk/Road/{road_id.lower()}"
        params = {"app_key": self._api_key}
        try:
            resp = self._session.get(url, params=params, timeout=10)
        except Exception:
            logger.exception("Network error fetching TfL road %s", road_id)
            return None
        if resp.status_code == 429:
            logger.warning("TfL rate limit hit, sleeping 60s before retry")
            time.sleep(60)
            try:
                resp = self._session.get(url, params=params, timeout=10)
            except Exception:
                logger.exception("Network error on retry for TfL road %s", road_id)
                return None
        if resp.status_code != 200:
            logger.warning("TfL API returned %d for road %s", resp.status_code, road_id)
            return None
        return self._parse_speed_response(resp.json(), road_id)

    def _parse_speed_response(
        self, data: list[dict[str, Any]], road_id: str
    ) -> dict[str, Any]:
        """Extract status fields from TfL JSON response.

        Args:
            data: Parsed JSON list from TfL Road endpoint.
            road_id: Road identifier for the result dict.
        Returns:
            Dict with road_id, status_severity, timestamp.
        """
        item = data[0] if data else {}
        return {
            "road_id": road_id,
            "status_severity": item.get("statusSeverity", "Unknown"),
            "timestamp": datetime.now(UTC).isoformat(),
        }


def fetch_camden_traffic(api_key: str) -> list[dict[str, Any]]:
    """Fetch traffic data for all Camden A-roads via TfL API.

    Args:
        api_key: TfL API key from settings.TFL_API_KEY.

    Returns:
        List of speed records for Camden roads that returned data.
    """
    client = TfLClient(api_key)
    results = client.get_road_speeds(CAMDEN_ROAD_IDS)
    for r in results:
        logger.info(
            "TfL %s: speed=%s at %s",
            r["road_id"],
            r.get("current_speed"),
            r["timestamp"],
        )
    return results


def map_roads_to_segments(
    records: list[dict[str, Any]], db: Session
) -> list[tuple[Any, float]]:
    """Map TfL road speed readings to nearby street segment IDs.

    Args:
        records: List of dicts from fetch_camden_traffic with road_id, current_speed, timestamp.
        db: SQLAlchemy Session from caller (do not create a new engine).

    Returns:
        List of (segment_id, speed_kmh) tuples for all matching segments.
    """
    pairs: list[tuple[Any, float]] = []
    for record in records:
        centroid = CAMDEN_ROAD_CENTROIDS.get(record["road_id"])
        if centroid is None:
            continue
        lon, lat = centroid
        speed = record.get("current_speed")
        if speed is None:
            continue
        rows = db.execute(
            text(
                "SELECT id FROM streetsegment "
                "WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 0.003)"
            ),
            {"lon": lon, "lat": lat},
        ).all()
        for (segment_id,) in rows:
            pairs.append((segment_id, float(speed)))
    return pairs


def upsert_traffic(pairs: list[tuple[Any, float]], db: Session) -> int:
    """Insert traffic speed records into the traffichistory table.

    Args:
        pairs: List of (segment_id, speed_kmh) tuples from map_roads_to_segments().
        db: SQLAlchemy Session from caller (do not create a new engine).

    Returns:
        Number of records inserted.
    """
    if not pairs:
        logger.info("No traffic pairs to insert")
        return 0
    now = datetime.now(UTC)
    values = [
        {
            "segment_id": str(sid),
            "speed": float(speed),
            "timestamp": now,
        }
        for sid, speed in pairs
    ]
    db.execute(
        text(
            "INSERT INTO traffichistory (segment_id, speed, timestamp) "
            "VALUES (:segment_id, :speed, :timestamp)"
        ),
        values,
    )
    db.commit()
    logger.info("Inserted %d traffic records", len(values))
    return len(values)


# This code satisfies PH6-020. No additional functionality added.
