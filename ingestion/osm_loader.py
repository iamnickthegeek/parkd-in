"""
OSM road network loader for Camden.

Fetches Camden road network via osmnx with a 7-day Parquet cache at
predictive_parking/cache/camden_osm.parquet. Splits long edges into 50–100m
segments and upserts to streetsegment table.

External dependencies:
    - osmnx (Overpass API)
    - geopandas
    - shapely
    - sqlalchemy
State:
    - Cache file on local filesystem (predictive_parking/cache/camden_osm.parquet).
"""

import logging
import pathlib
import uuid
from typing import Any

import geopandas as gpd  # type: ignore[import-untyped]
import osmnx as ox
from shapely.ops import substring  # type: ignore[import-untyped]
from sqlalchemy import select, text as sa_text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import StreetSegment

logger = logging.getLogger(__name__)

# Cache file location (7-day TTL)
_CACHE_PATH: pathlib.Path | None = None


def _cache_path() -> pathlib.Path:
    """Return the Parquet cache path, creating cache directory if needed."""
    global _CACHE_PATH
    if _CACHE_PATH is None:
        cache_dir = pathlib.Path(__file__).parent.parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        _CACHE_PATH = cache_dir / "camden_osm.parquet"
    return _CACHE_PATH


def fetch_camden_graph() -> gpd.GeoDataFrame:
    """Fetch Camden road network from Overpass API or local Parquet cache.

    Returns:
        GeoDataFrame of road edges in EPSG:27700 (British National Grid).
        Columns include: osmid, name, highway, geometry.

    Raises:
        Exception: If Overpass download fails (cache miss and no cached data).
    """
    cache_file = _cache_path()
    use_cache = False

    if cache_file.exists():
        cache_age = __import__("time").time() - cache_file.stat().st_mtime
        if cache_age < 7 * 86400:  # 7 days
            use_cache = True

    if use_cache:
        try:
            gdf = gpd.read_parquet(cache_file)
            logger.info("Loaded OSM graph from cache (%s edges)", len(gdf))
            return gdf
        except Exception as e:
            logger.warning("Cache read failed, falling back to download: %s", e)

    # Download from Overpass
    try:
        G = ox.graph_from_place("London Borough of Camden, UK", network_type="drive")
        gdf = ox.graph_to_gdfs(G, nodes=False)
        gdf = gdf.to_crs(epsg=27700)

        # Keep only essential columns and normalize list-valued columns
        def normalize_list(val: Any) -> Any:
            import math

            if isinstance(val, (list, tuple)):
                return val[0] if val else None
            if isinstance(val, float) and math.isnan(val):
                return None
            return val

        # Only keep columns we need for segmentation
        keep_cols = ["osmid", "name", "highway", "geometry"]
        gdf = gdf[keep_cols].copy()

        for col in ["osmid", "name", "highway"]:
            gdf[col] = gdf[col].apply(normalize_list)

        gdf.to_parquet(cache_file)
        logger.info("Downloaded Camden OSM graph (%s edges)", len(gdf))
        return gdf
    except Exception:
        logger.exception("Failed to download OSM graph from Overpass")
        raise


def segment_edges(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Split long road edges into 50–100m segments.

    Args:
        gdf: Road edges GeoDataFrame in EPSG:27700.

    Returns:
        GeoDataFrame in EPSG:4326 with columns:
        - osm_id (int64): original OSM way ID
        - street_name (str | None): road name
        - road_type (str | None): highway classification
        - length_m (float): segment length in metres
        - geometry (LineString): segment geometry in EPSG:4326.
    """
    segments: list[dict[str, Any]] = []

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue

        length_m = geom.length
        osmid = row.get("osmid")
        # osmid may be a list (parallel edges) — take first
        if isinstance(osmid, (list, tuple)):
            osmid = osmid[0] if osmid else None

        name = row.get("name")
        highway = row.get("highway")

        if length_m > 100:
            # Split into ~75m chunks
            num_chunks = int(length_m // 75) + 1
            chunk_len = length_m / num_chunks
            for i in range(num_chunks):
                start = i * chunk_len
                end = min((i + 1) * chunk_len, length_m)
                sub_geom = substring(geom, start, end)
                # Only the first sub-segment keeps the osm_id; others get NULL
                sub_osmid = osmid if i == 0 else None
                segments.append(
                    {
                        "osm_id": sub_osmid,
                        "street_name": name,
                        "road_type": highway,
                        "length_m": end - start,
                        "geometry": sub_geom,
                    }
                )
        else:
            segments.append(
                {
                    "osm_id": osmid,
                    "street_name": name,
                    "road_type": highway,
                    "length_m": length_m,
                    "geometry": geom,
                }
            )

    result = gpd.GeoDataFrame(segments, crs=gdf.crs)
    result = result.to_crs(epsg=4326)
    return result


def upsert_segments(gdf: gpd.GeoDataFrame, db: Any) -> tuple[int, int]:
    """Upsert road segments into streetsegment table using batch operations.

    Args:
        gdf: Segmented GeoDataFrame in EPSG:4326 from segment_edges().
        db: SQLAlchemy Session (caller manages lifecycle).

    Returns:
        Tuple (inserted_count, updated_count).
    """
    import math

    # Build rows, separating by osm_id presence
    rows_with_osm: list[dict[str, Any]] = []
    rows_without_osm: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        osm_id = row.osm_id
        if isinstance(osm_id, float) and math.isnan(osm_id):
            osm_id = None
        row_dict = {
            "id": uuid.uuid4(),
            "osm_id": osm_id,
            "street_name": row.street_name,
            "road_type": row.road_type,
            "length_m": row.length_m,
            "geom": f"SRID=4326;{row.geometry.wkt}",
            "estimated_spaces": 0,
        }
        if osm_id is not None:
            rows_with_osm.append(row_dict)
        else:
            rows_without_osm.append(row_dict)

    if not rows_with_osm and not rows_without_osm:
        return (0, 0)

    # Step 1: Find which osm_ids already exist
    existing_osm_ids: set[int] = set()
    if rows_with_osm:
        osm_values = [r["osm_id"] for r in rows_with_osm]
        # Process in chunks to avoid SQL parameter limits
        chunk_size = 1000
        for i in range(0, len(osm_values), chunk_size):
            chunk = osm_values[i : i + chunk_size]
            existing = (
                db.execute(
                    select(StreetSegment.osm_id).where(StreetSegment.osm_id.in_(chunk))
                )
                .scalars()
                .all()
            )
            existing_osm_ids.update(existing)

    # Step 2: Split rows_with_osm into updates vs inserts
    update_rows: list[dict[str, Any]] = []
    new_insert_rows: list[dict[str, Any]] = []
    for row_dict in rows_with_osm:
        if row_dict["osm_id"] in existing_osm_ids:
            update_rows.append(row_dict)
        else:
            new_insert_rows.append(row_dict)

    # Step 3: Batch update using unnest() for efficiency
    updated = len(update_rows)
    if update_rows:
        chunk_size = 500
        for i in range(0, len(update_rows), chunk_size):
            chunk = update_rows[i : i + chunk_size]
            osm_ids = [r["osm_id"] for r in chunk]
            street_names = [r["street_name"] for r in chunk]
            road_types = [r["road_type"] for r in chunk]
            lengths = [r["length_m"] for r in chunk]
            geoms = [r["geom"] for r in chunk]
            db.execute(
                sa_text("""
                    UPDATE streetsegment s SET
                        street_name = u.street_name,
                        road_type = u.road_type,
                        length_m = u.length_m,
                        geom = u.geom
                    FROM unnest(
                        CAST(:osm_ids AS bigint[]),
                        CAST(:street_names AS text[]),
                        CAST(:road_types AS text[]),
                        CAST(:lengths AS float[]),
                        CAST(:geoms AS text[])
                    ) AS u(osm_id, street_name, road_type, length_m, geom)
                    WHERE s.osm_id = u.osm_id
                """),
                {
                    "osm_ids": osm_ids,
                    "street_names": street_names,
                    "road_types": road_types,
                    "lengths": lengths,
                    "geoms": geoms,
                },
            )

    # Step 4: Batch insert all new rows (with and without osm_id)
    all_inserts = new_insert_rows + rows_without_osm
    inserted = 0
    if all_inserts:
        chunk_size = 500
        for i in range(0, len(all_inserts), chunk_size):
            chunk = all_inserts[i : i + chunk_size]
            db.execute(insert(StreetSegment), chunk)
            inserted += len(chunk)

    db.commit()
    logger.info("Upserted segments: %s inserted, %s updated", inserted, updated)
    return (inserted, updated)
