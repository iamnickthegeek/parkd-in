"""
Local tile generator — no Supabase, no R2, no Redis required.

Reads the OSM road network from the local Parquet cache, assigns a
heuristic parking probability to every segment, and writes .pbf tiles
to frontend/tiles/{z}/{x}/{y}.pbf so they can be served by the FastAPI
backend in LOCAL_MODE or directly by python -m http.server.

Usage:
    cd predictive_parking
    python generate_tiles_local.py

Output:
    frontend/tiles/14/<x>/<y>.pbf  (30 tiles covering Camden at zoom 14)
"""

import math
import pathlib
import struct
import sys

# ---------------------------------------------------------------------------
# Deps check
# ---------------------------------------------------------------------------
try:
    import geopandas as gpd
    import mapbox_vector_tile
    from shapely.geometry import box, mapping
    from shapely.ops import substring, transform as shapely_transform
    import pyproj
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install geopandas mapbox-vector-tile pyproj shapely pyarrow")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CACHE_PATH = pathlib.Path(__file__).parent / "cache" / "camden_osm.parquet"
TILE_DIR = pathlib.Path(__file__).parent / "frontend" / "tiles"
ZOOM = 14

# Camden tile grid at zoom 14 (same as engine.py TILE_GRID)
TILE_GRID = [(ZOOM, x, y) for x in range(8183, 8189) for y in range(5443, 5448)]

# Colour constants (hex int) matching engine.py
GREEN  = 0x00AA00  # prob >= 0.7
YELLOW = 0xFFAA00  # prob >= 0.3
RED    = 0xCC0000  # prob < 0.3

# ---------------------------------------------------------------------------
# Tile math helpers
# ---------------------------------------------------------------------------
def tile_bounds_wgs84(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) in WGS84 degrees for a tile."""
    n = 2 ** z
    west  = x / n * 360.0 - 180.0
    east  = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (west, lat_s, east, lat_n)


def wgs84_to_tile_coords(
    lon: float, lat: float,
    west: float, south: float, east: float, north: float,
    extent: int = 4096,
) -> tuple[int, int]:
    """Map WGS84 lon/lat to tile pixel coordinates (origin = top-left)."""
    tx = int((lon - west) / (east - west) * extent)
    ty = int((north - lat) / (north - south) * extent)
    return (tx, ty)


def linestring_to_tile_coords(
    geom,  # shapely LineString in WGS84
    west: float, south: float, east: float, north: float,
    extent: int = 4096,
) -> list[tuple[int, int]]:
    """Convert a WGS84 LineString to tile pixel coordinate list."""
    coords = []
    for lon, lat in geom.coords:
        tx, ty = wgs84_to_tile_coords(lon, lat, west, south, east, north, extent)
        coords.append((tx, ty))
    return coords


# ---------------------------------------------------------------------------
# Load + prepare segments
# ---------------------------------------------------------------------------
def load_segments() -> gpd.GeoDataFrame:
    """Load cached OSM parquet, split long edges, reproject to WGS84."""
    print(f"Loading OSM cache from {CACHE_PATH}...")
    gdf = gpd.read_parquet(str(CACHE_PATH))  # EPSG:27700

    # Split edges > 100m into ~75m chunks (mirrors osm_loader.segment_edges)
    segments = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        length = geom.length
        name = row.get("name")
        highway = row.get("highway")
        if isinstance(name, list):
            name = name[0] if name else None
        if isinstance(highway, list):
            highway = highway[0] if highway else None

        if length > 100:
            n_chunks = int(length // 75) + 1
            chunk_len = length / n_chunks
            for i in range(n_chunks):
                sub = substring(geom, i * chunk_len, min((i + 1) * chunk_len, length))
                segments.append({"street_name": name, "highway": highway,
                                  "length_m": chunk_len, "geometry": sub})
        else:
            segments.append({"street_name": name, "highway": highway,
                              "length_m": length, "geometry": geom})

    result = gpd.GeoDataFrame(segments, crs=gdf.crs)
    result = result.to_crs(epsg=4326)
    print(f"  {len(result)} segments after splitting")
    return result


# ---------------------------------------------------------------------------
# Probability heuristic (pure Python, no DB)
# ---------------------------------------------------------------------------
def assign_probabilities(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Assign a deterministic heuristic probability to each segment.

    Uses road type as a proxy for parking availability:
      residential / living_street / unclassified → high (green)
      tertiary / secondary                        → medium (yellow)
      primary / trunk / motorway / cycleway       → low (red)
    """
    def _prob(highway: str | None) -> float:
        h = str(highway).lower() if highway else ""
        if h in ("residential", "living_street", "unclassified", "service"):
            return 0.75
        if h in ("tertiary", "tertiary_link", "secondary", "secondary_link"):
            return 0.50
        # primary, trunk, motorway, cycleway, footway — low parking chance
        return 0.25

    gdf = gdf.copy()
    gdf["prob"] = gdf["highway"].apply(_prob)
    gdf["color"] = gdf["prob"].apply(
        lambda p: "00AA00" if p >= 0.7 else ("FFAA00" if p >= 0.3 else "CC0000")
    )
    return gdf


# ---------------------------------------------------------------------------
# MVT generation
# ---------------------------------------------------------------------------
def build_tile(
    gdf: gpd.GeoDataFrame,
    z: int, x: int, y: int,
) -> bytes | None:
    """Build a Mapbox Vector Tile for the given tile coords.

    Args:
        gdf: WGS84 GeoDataFrame with prob and color columns.
        z, x, y: Tile coordinates.

    Returns:
        Raw .pbf bytes, or None if no features intersect the tile.
    """
    west, south, east, north = tile_bounds_wgs84(z, x, y)
    tile_box = box(west, south, east, north)

    # Filter to segments that intersect this tile
    candidates = gdf[gdf.geometry.intersects(tile_box)]
    if candidates.empty:
        return None

    features = []
    for _, row in candidates.iterrows():
        geom = row.geometry
        # Clip to tile bounds
        clipped = geom.intersection(tile_box)
        if clipped.is_empty:
            continue

        # Handle MultiLineString after clip
        if clipped.geom_type == "MultiLineString":
            geoms = list(clipped.geoms)
        elif clipped.geom_type == "LineString":
            geoms = [clipped]
        else:
            continue

        for g in geoms:
            if len(list(g.coords)) < 2:
                continue
            # Pass WGS84 coordinates — quantize_bounds handles projection
            features.append({
                "geometry": mapping(g),
                "properties": {
                    "color": row["color"],
                    "prob": int(row["prob"] * 100),
                    "street_name": row.get("street_name") or "",
                },
            })

    if not features:
        return None

    # quantize_bounds projects WGS84 lon/lat → tile pixel space [0, extents]
    tile_data = [{"name": "parking", "features": features}]
    try:
        pbf = mapbox_vector_tile.encode(
            tile_data,
            default_options={
                "extents": 4096,
                "quantize_bounds": (west, south, east, north),
                "y_coord_down": True,
            },
        )
        return pbf
    except Exception as e:
        print(f"    encode error for {z}/{x}/{y}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    gdf = load_segments()
    gdf = assign_probabilities(gdf)

    counts = gdf["color"].value_counts().to_dict()
    print(f"Segment colours: green={counts.get('00AA00',0)}, "
          f"yellow={counts.get('FFAA00',0)}, red={counts.get('CC0000',0)}")

    written = 0
    skipped = 0
    for z, x, y in TILE_GRID:
        tile_path = TILE_DIR / str(z) / str(x) / f"{y}.pbf"
        tile_path.parent.mkdir(parents=True, exist_ok=True)

        pbf = build_tile(gdf, z, x, y)
        if pbf:
            tile_path.write_bytes(pbf)
            written += 1
        else:
            # Write empty tile if one existed before (avoids 404 noise)
            if tile_path.exists():
                tile_path.unlink()
            skipped += 1

    print(f"\nDone: {written} tiles written, {skipped} empty tiles skipped")
    print(f"Tile directory: {TILE_DIR.resolve()}")
    print("\nNext steps:")
    print("  1. Set LOCAL_MODE=true in .env")
    print("  2. uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload")
    print("  3. Open http://localhost:3000  (tiles served via /api/v1/tiles/)")


if __name__ == "__main__":
    main()
