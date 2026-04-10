"""
Geometry sync: aligns streetsegment.geom to Mapbox Streets v8 centerlines.

Mapbox Streets v8 and the road geometry downloaded by osmnx both derive from
OpenStreetMap, but Mapbox applies its own geometry refinement that can shift
road centerlines by up to ~20m — enough to look visually wrong at zoom 14.

This module fixes that by:
  1. Downloading Mapbox Streets v8 vector tiles for the Camden tile grid.
  2. Decoding road features and converting pixel coordinates → WGS84.
  3. Loading the aligned centerlines into a PostGIS staging table.
  4. Updating streetsegment.geom for any row whose centroid is within 30m of
     a Mapbox centerline (conservative threshold: real misalignment is 5–20m;
     30m avoids matching across different streets).
  5. Re-linking parking_bay rows to the nearest corrected segment.

Run once after the initial osmnx import, then weekly (roads rarely move).

Usage:
    python -m ingestion.mapbox_geometry

External dependencies: requests, mapbox-vector-tile, sqlalchemy
"""

import logging
import math
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    import mapbox_vector_tile
    import requests
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    print("Run: pip install mapbox-vector-tile requests")
    sys.exit(1)

from sqlalchemy import text
from sqlalchemy.orm import Session

# Camden tile grid at zoom 14 (same as engine.py TILE_GRID)
TILE_GRID: list[tuple[int, int, int]] = [
    (14, x, y) for x in range(8183, 8189) for y in range(5443, 5448)
]

MAPBOX_TILESET = "mapbox.mapbox-streets-v8"

# Road classes that can have on-street parking in Camden.
# Excludes motorway, motorway_link, pedestrian, path, footway, etc.
PARK_CLASSES: frozenset[str] = frozenset({
    "street",
    "street_limited",
    "service",
    "tertiary",
    "secondary",
    "secondary_link",
    "primary",
    "primary_link",
    "trunk",
    "trunk_link",
})

# Maximum distance (metres) between OSM segment centroid and Mapbox centroid
# to qualify as the same road and be updated.
MATCH_THRESHOLD_M = 30


def pixel_to_lnglat(
    px: float, py: float, z: int, tx: int, ty: int, extent: int = 4096
) -> tuple[float, float]:
    """Convert Mapbox Vector Tile pixel coordinates to WGS84 (lng, lat).

    In MVT spec pixel (0, 0) is the top-left corner of the tile.
    x increases east, y increases south (screen coordinates).

    Args:
        px: Pixel x coordinate in [0, extent].
        py: Pixel y coordinate in [0, extent].
        z: Zoom level.
        tx: Tile column index.
        ty: Tile row index.
        extent: Tile extent (default 4096 for Mapbox Streets).

    Returns:
        Tuple of (longitude, latitude) in WGS84 degrees.
    """
    n = 2.0 ** z
    lng = (tx + px / extent) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * (ty + py / extent) / n)))
    return lng, math.degrees(lat_rad)


def _download_tile(z: int, x: int, y: int, token: str) -> bytes | None:
    """Download a single Mapbox vector tile with retry.

    Args:
        z: Zoom level.
        x: Tile column.
        y: Tile row.
        token: Mapbox public access token.

    Returns:
        Raw PBF bytes, or None if all attempts failed.
    """
    url = (
        f"https://api.mapbox.com/v4/{MAPBOX_TILESET}/{z}/{x}/{y}.vector.pbf"
        f"?access_token={token}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            logger.warning("HTTP %d for tile %d/%d/%d", resp.status_code, z, x, y)
            return None
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                logger.error(
                    "Tile %d/%d/%d failed after 3 attempts: %s", z, x, y, exc
                )
    return None


def _extract_linestrings(
    pbf: bytes, z: int, tx: int, ty: int
) -> list[list[tuple[float, float]]]:
    """Extract WGS84 LineString coordinates from road features in one tile.

    Args:
        pbf: Raw PBF bytes of one Mapbox vector tile.
        z: Zoom level.
        tx: Tile column.
        ty: Tile row.

    Returns:
        List of coordinate sequences, each a list of (lng, lat) tuples.
    """
    decoded = mapbox_vector_tile.decode(pbf)
    if "road" not in decoded:
        return []

    lines: list[list[tuple[float, float]]] = []
    for feature in decoded["road"]["features"]:
        cls = feature["properties"].get("class", "")
        if cls not in PARK_CLASSES:
            continue
        geom = feature["geometry"]
        coords = geom["coordinates"]
        if geom["type"] == "LineString" and len(coords) >= 2:
            lines.append(
                [pixel_to_lnglat(px, py, z, tx, ty) for px, py in coords]
            )
        elif geom["type"] == "MultiLineString":
            for seg in coords:
                if len(seg) >= 2:
                    lines.append(
                        [pixel_to_lnglat(px, py, z, tx, ty) for px, py in seg]
                    )
    return lines


def _linestring_to_wkt(coords: list[tuple[float, float]]) -> str:
    pts = ", ".join(f"{lng} {lat}" for lng, lat in coords)
    return f"LINESTRING({pts})"


def sync_geometries(db: Session, mapbox_token: str) -> dict[str, int]:
    """Download Mapbox road centerlines and update streetsegment.geom.

    Uses a PostGIS staging table to match Mapbox centerlines to existing
    streetsegment rows by centroid proximity, then updates geom in-place.
    All linked data (parking_bay, traffichistory, enforcement) is preserved.

    After updating segment geometries, re-runs the nearest-segment assignment
    for parking_bay rows so bay→segment links stay accurate.

    Args:
        db: SQLAlchemy Session. Must be a direct connection (not PgBouncer) if
            the caller needs TEMP TABLE visibility — pass a NullPool engine.
        mapbox_token: Mapbox public access token.

    Returns:
        Dict with keys: tiles_downloaded, linestrings_inserted,
        segments_updated, bays_relinked.
    """
    db.execute(text("SET statement_timeout = '600000'"))

    # ------------------------------------------------------------------ #
    # Step 1: staging table                                                #
    # ------------------------------------------------------------------ #
    db.execute(text("DROP TABLE IF EXISTS tmp_mapbox_geom"))
    db.execute(
        text(
            "CREATE TEMP TABLE tmp_mapbox_geom ("
            "  id    SERIAL PRIMARY KEY,"
            "  geom  GEOMETRY(LINESTRING, 4326)"
            ")"
        )
    )

    # ------------------------------------------------------------------ #
    # Step 2: download tiles and insert linestrings                        #
    # ------------------------------------------------------------------ #
    total_lines = 0
    tiles_downloaded = 0

    for z, tx, ty in TILE_GRID:
        pbf = _download_tile(z, tx, ty, mapbox_token)
        if pbf is None:
            continue
        tiles_downloaded += 1
        lines = _extract_linestrings(pbf, z, tx, ty)
        for coords in lines:
            wkt = _linestring_to_wkt(coords)
            db.execute(
                text(
                    "INSERT INTO tmp_mapbox_geom (geom) "
                    "VALUES (ST_GeomFromText(:wkt, 4326))"
                ),
                {"wkt": wkt},
            )
            total_lines += 1
        logger.info("Tile %d/%d/%d: %d linestrings", z, tx, ty, len(lines))

    logger.info(
        "Inserted %d Mapbox linestrings from %d tiles", total_lines, tiles_downloaded
    )

    # ------------------------------------------------------------------ #
    # Step 3: spatial index on staging table                               #
    # ------------------------------------------------------------------ #
    db.execute(text("CREATE INDEX ON tmp_mapbox_geom USING GIST (geom)"))
    db.execute(text("ANALYZE tmp_mapbox_geom"))

    # ------------------------------------------------------------------ #
    # Step 4: update streetsegment.geom                                    #
    #                                                                      #
    # For each streetsegment, find the single Mapbox linestring whose       #
    # centroid is closest to the segment centroid, within MATCH_THRESHOLD_M. #
    # Use DISTINCT ON to pick one Mapbox line per segment (nearest first).  #
    # ------------------------------------------------------------------ #
    result = db.execute(
        text(
            f"""
            WITH matched AS (
                SELECT DISTINCT ON (s.id)
                    s.id   AS seg_id,
                    m.geom AS new_geom
                FROM streetsegment s
                JOIN tmp_mapbox_geom m
                  ON ST_DWithin(
                         ST_Centroid(s.geom)::geography,
                         ST_Centroid(m.geom)::geography,
                         {MATCH_THRESHOLD_M}
                     )
                ORDER BY
                    s.id,
                    ST_Distance(
                        ST_Centroid(s.geom)::geography,
                        ST_Centroid(m.geom)::geography
                    )
            )
            UPDATE streetsegment s
            SET    geom = matched.new_geom
            FROM   matched
            WHERE  s.id = matched.seg_id
            """
        )
    )
    segments_updated = result.rowcount
    logger.info("Updated %d streetsegment geometries to Mapbox alignment", segments_updated)

    # ------------------------------------------------------------------ #
    # Step 5: re-link parking_bay to nearest corrected segment             #
    # ------------------------------------------------------------------ #
    result2 = db.execute(
        text(
            """
            WITH nearest AS (
                SELECT DISTINCT ON (b.id)
                    b.id AS bay_id,
                    s.id AS seg_id
                FROM parking_bay b
                JOIN streetsegment s
                  ON ST_DWithin(b.geom::geography, s.geom::geography, 30)
                ORDER BY
                    b.id,
                    ST_Distance(b.geom::geography, s.geom::geography)
            )
            UPDATE parking_bay b
            SET    segment_id = nearest.seg_id
            FROM   nearest
            WHERE  b.id = nearest.bay_id
              AND  b.segment_id IS DISTINCT FROM nearest.seg_id
            """
        )
    )
    bays_relinked = result2.rowcount
    logger.info("Relinked %d parking bays to updated segments", bays_relinked)

    db.execute(text("DROP TABLE IF EXISTS tmp_mapbox_geom"))
    db.commit()

    return {
        "tiles_downloaded": tiles_downloaded,
        "linestrings_inserted": total_lines,
        "segments_updated": segments_updated,
        "bays_relinked": bays_relinked,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, "backend")

    from dotenv import load_dotenv
    load_dotenv(".env")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    db_url = os.environ["SUPABASE_DATABASE_URL"].split("?")[0]
    # Direct connection (port 5432) required for TEMP TABLE visibility
    if ":6543" in db_url:
        db_url = db_url.replace(":6543", ":5432")

    token = os.environ["MAPBOX_TOKEN"]

    engine = create_engine(db_url, poolclass=NullPool,
                           connect_args={"connect_timeout": 15, "sslmode": "disable"})
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        stats = sync_geometries(session, token)
        print("Geometry sync complete:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
    finally:
        session.close()
        engine.dispose()
