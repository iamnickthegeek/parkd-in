"""
This script performs a one-off spatial data ingestion into the PostGIS database.
It processes road segments and parking bay capacity data using GeoPandas.

External Dependencies:
- geopandas
- sqlalchemy
- psycopg2-binary
- shapely
"""

import os
import uuid
import sys
from pathlib import Path
import geopandas as gpd  # type: ignore[import-untyped]
from sqlalchemy import Engine, create_engine
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

# Ensure the script can import local config if needed
# For simplicity in this one-off script, we'll re-extract DB DSN from env if present,
# or use defaults that match the project's docker-compose / .env.example.


def get_engine() -> Engine:
    """Builds a SQLAlchemy engine from SUPABASE_DATABASE_URL env var.

    Strips the pgbouncer=true parameter which is not accepted by psycopg2
    directly but is required by SQLAlchemy's PgBouncer-aware pool at runtime.
    """
    url = os.getenv("SUPABASE_DATABASE_URL", "")
    if not url:
        print("Error: SUPABASE_DATABASE_URL is not set in environment.")
        sys.exit(1)
    # psycopg2 rejects the pgbouncer param in the DSN; strip it for this script.
    url = url.replace("?pgbouncer=true", "").strip('"')
    return create_engine(url)


def process_spatial_data(roads_path: Path, bays_path: Path) -> gpd.GeoDataFrame:
    """
    Loads and processes spatial data.
    Returns a GeoDataFrame ready for database ingestion.
    """
    print("Loading GeoJSON data...")
    roads = gpd.read_file(roads_path)
    bays = gpd.read_file(bays_path)

    # Ensure consistent CRS (WGS84)
    roads = roads.to_crs(epsg=4326)
    bays = bays.to_crs(epsg=4326)

    print("Performing spatial join...")
    # Project to a local CRS (British National Grid) for accurate spatial join
    roads_projected = roads.to_crs(epsg=27700)
    bays_projected = bays.to_crs(epsg=27700)
    # sjoin_nearest finds the road closest to each bay.
    joined = gpd.sjoin_nearest(
        bays_projected, roads_projected, max_distance=50, distance_col="dist"
    )

    print("Aggregating capacity...")
    # Group by the roads' index
    road_capacities = joined.groupby("index_right")["capacity"].sum().reset_index()

    # Merge the capacities back onto the original roads GeoDataFrame
    final_segments = roads.copy()
    final_segments = final_segments.merge(
        road_capacities, left_index=True, right_on="index_right", how="left"
    )

    # Fill NaN capacities with 0 and rename to match database model
    final_segments["estimated_spaces"] = (
        final_segments["capacity"].fillna(0).astype(int)
    )

    # Prepare columns for database: id (uuid), geom, estimated_spaces
    final_segments["id"] = [uuid.uuid4() for _ in range(len(final_segments))]

    # We only want the required columns; set_geometry after rename keeps GeoPandas happy.
    db_ready = final_segments[["id", "geometry", "estimated_spaces"]].rename(
        columns={"geometry": "geom"}
    )
    db_ready = db_ready.set_geometry("geom")
    return db_ready


def init_spatial_data() -> None:
    """
    Main ingestion routine:
    1. Loads roads.geojson and bays.geojson.
    2. Performs spatial join to assign bays to the nearest road.
    3. Aggregates bay capacity per road.
    4. Writes result to the 'streetsegment' table in PostGIS.
    """
    script_dir = Path(__file__).parent
    roads_path = script_dir / "roads.geojson"
    bays_path = script_dir / "bays.geojson"

    if not roads_path.exists() or not bays_path.exists():
        print(f"Error: Required geojson files not found in {script_dir}")
        sys.exit(1)

    db_ready = process_spatial_data(roads_path, bays_path)

    print(f"Ingesting {len(db_ready)} segments into database...")
    engine = get_engine()

    try:
        # Pushes to 'streetsegment' table.
        db_ready.to_postgis("streetsegment", engine, if_exists="append", index=False)
        print("Successfully ingested spatial data.")
    except Exception as e:
        print(f"Error during ingestion: {e}")
        sys.exit(1)


if __name__ == "__main__":
    init_spatial_data()
