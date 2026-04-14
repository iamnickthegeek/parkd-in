"""
Parkd-In tile generator (local / Vercel deploy).

Downloads road geometry directly from the Mapbox Streets v8 vector tiles and
re-encodes them with parking-probability colours.  Because the source geometry
IS the same data Mapbox uses to render the base map, the coloured segments are
guaranteed to sit exactly on the roads.

Also writes public/segments.json — a list of road centroids with base
probabilities.  The frontend reads this file to render ranked "P" markers
without any backend API calls (no Supabase, no R2, no Redis required).

Usage:
    cd predictive_parking
    python generate_tiles_local.py

Output:
    public/tiles/14/<x>/<y>.pbf  — 30 tiles covering Camden at zoom 14
    public/segments.json          — segment centroids + base probabilities
"""

import json
import math
import os
import pathlib
import sys
import time

try:
    import mapbox_vector_tile
    import requests
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install mapbox-vector-tile requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TILE_DIR = pathlib.Path(__file__).parent / "public" / "tiles"
SEGMENTS_FILE = pathlib.Path(__file__).parent / "public" / "segments.json"
ZOOM = 14

# Camden tile grid at zoom 14
TILE_GRID = [(ZOOM, x, y) for x in range(8183, 8189) for y in range(5443, 5448)]

# Public browser token (URL-restricted to predictiveparking.vercel.app).
# For server-side tile generation, override via MAPBOX_TOKEN env var using
# an unrestricted token — Mapbox rejects URL-restricted tokens from requests
# that carry no Referer header (e.g. GitHub Actions, local scripts).
_BROWSER_TOKEN = (
    "pk.eyJ1Ijoibmlja3RoZWdlZWsiLCJhIjoiY21udTZ5YTVkMDhuejJxc2N6MjZzb2dwNSJ9"
    ".GNQEyAD5HwhNb42cznksOQ"
)
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", _BROWSER_TOKEN)
MAPBOX_TILESET = "mapbox.mapbox-streets-v8"

# Road class → (hex color for tile overlay, base probability 0–100).
# Base probability reflects typical parking availability by road type in Camden,
# ignoring time of day.  The frontend applies a time-of-day multiplier on top.
PARK_CLASSES: dict[str, tuple[str, int]] = {
    "street":         ("00AA00", 75),   # residential streets — generally available
    "street_limited": ("FFAA00", 50),   # limited-access local streets
    "service":        ("00AA00", 70),   # service roads, car parks
    "tertiary":       ("FFAA00", 50),   # tertiary roads
    "secondary":      ("FFAA00", 45),   # secondary roads
    "primary":        ("CC0000", 20),   # primary roads — usually yellow lines
    "primary_link":   ("CC0000", 20),
    "trunk":          ("CC0000", 10),   # trunk roads — very restricted
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def pixel_to_lnglat(
    px: float, py: float, z: int, tx: int, ty: int, extent: int = 4096
) -> tuple[float, float]:
    """Convert MVT pixel coordinates to WGS84 (lng, lat).

    Pixel (0, 0) is the top-left of the tile; y increases southward.
    """
    n = 2.0 ** z
    lng = (tx + px / extent) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * (ty + py / extent) / n)))
    return lng, math.degrees(lat_rad)


def linestring_midpoint(
    coords: list, z: int, tx: int, ty: int
) -> tuple[float, float]:
    """Return the WGS84 midpoint (lng, lat) of a pixel-space linestring."""
    mid = coords[len(coords) // 2]
    return pixel_to_lnglat(mid[0], mid[1], z, tx, ty)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_tile(z: int, x: int, y: int) -> bytes | None:
    url = (
        f"https://api.mapbox.com/v4/{MAPBOX_TILESET}/{z}/{x}/{y}.vector.pbf"
        f"?access_token={MAPBOX_TOKEN}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            print(f"  HTTP {resp.status_code} for {z}/{x}/{y}")
            return None
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"  Failed after 3 attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# Process tile → PBF bytes
# ---------------------------------------------------------------------------

def build_parking_tile(mapbox_pbf: bytes) -> bytes | None:
    """Re-encode Mapbox road features with parking colours.

    The road pixel coordinates come straight from the Mapbox tile — no
    re-projection step — so they align perfectly with the base map.
    """
    decoded = mapbox_vector_tile.decode(mapbox_pbf)
    if "road" not in decoded:
        return None

    features = []
    for f in decoded["road"]["features"]:
        cls = f["properties"].get("class", "")
        if cls not in PARK_CLASSES:
            continue

        color, prob = PARK_CLASSES[cls]
        geom = f["geometry"]
        geom_type = geom["type"]
        coords = geom["coordinates"]

        lines: list[list] = []
        if geom_type == "LineString":
            if len(coords) >= 2:
                lines = [coords]
        elif geom_type == "MultiLineString":
            lines = [seg for seg in coords if len(seg) >= 2]

        name = f["properties"].get("name") or f["properties"].get("name_en") or ""

        for line in lines:
            features.append({
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": {
                    "color": color,
                    "prob": prob,
                    "street_name": str(name),
                },
            })

    if not features:
        return None

    # Encode without quantize_bounds: coordinates are already in 0–4096 pixel
    # space from the Mapbox tile, so no re-projection is needed.
    return mapbox_vector_tile.encode(
        [{"name": "parking", "features": features}],
        default_options={"extents": 4096},
    )


# ---------------------------------------------------------------------------
# Process tile → segment records (for segments.json)
# ---------------------------------------------------------------------------

def extract_segments(
    mapbox_pbf: bytes, z: int, tx: int, ty: int
) -> list[dict]:
    """Return a list of segment dicts for segments.json from one tile.

    Each dict has: name, lat, lon, prob (0–100), road_class.
    lat/lon is the WGS84 midpoint of the linestring.
    """
    decoded = mapbox_vector_tile.decode(mapbox_pbf)
    if "road" not in decoded:
        return []

    segments = []
    for f in decoded["road"]["features"]:
        cls = f["properties"].get("class", "")
        if cls not in PARK_CLASSES:
            continue

        _, prob = PARK_CLASSES[cls]
        geom = f["geometry"]
        coords = geom["coordinates"]
        name = (
            f["properties"].get("name")
            or f["properties"].get("name_en")
            or ""
        )

        lines: list[list] = []
        if geom["type"] == "LineString" and len(coords) >= 2:
            lines = [coords]
        elif geom["type"] == "MultiLineString":
            lines = [seg for seg in coords if len(seg) >= 2]

        for line in lines:
            lng, lat = linestring_midpoint(line, z, tx, ty)
            segments.append({
                "n": str(name),       # street name
                "lat": round(lat, 5),
                "lon": round(lng, 5),
                "p": prob,            # base probability %
                "c": cls,             # road class
            })

    return segments


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deg_to_m(dlat: float, dlon: float, lat: float) -> float:
    """Rough metre distance for small lat/lon deltas."""
    return math.sqrt((dlat * 111320) ** 2 + (dlon * 111320 * math.cos(math.radians(lat))) ** 2)


def deduplicate_segments(segments: list[dict], threshold_m: float = 80.0) -> list[dict]:
    """Remove duplicate entries for the same named street within threshold_m.

    Roads that span multiple tiles appear once per tile.  We keep only the
    first occurrence within the threshold, since the frontend sorts by distance
    from the user anyway.

    Unnamed roads (n == "") are kept as-is (they can't be grouped by name).
    """
    seen: dict[str, tuple[float, float]] = {}  # name → (lat, lon) of first occurrence
    result = []

    for seg in segments:
        name = seg["n"]
        if not name:
            result.append(seg)
            continue

        if name in seen:
            prev_lat, prev_lon = seen[name]
            dist = _deg_to_m(seg["lat"] - prev_lat, seg["lon"] - prev_lon, seg["lat"])
            if dist < threshold_m:
                continue  # same road, close enough — skip

        seen[name] = (seg["lat"], seg["lon"])
        result.append(seg)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import datetime

    print(f"Downloading {len(TILE_GRID)} tiles from {MAPBOX_TILESET}...")
    written = skipped = failed = 0
    all_segments: list[dict] = []

    for z, x, y in TILE_GRID:
        tile_path = TILE_DIR / str(z) / str(x) / f"{y}.pbf"
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {z}/{x}/{y} ... ", end="", flush=True)

        raw = download_tile(z, x, y)
        if raw is None:
            print("FAILED")
            failed += 1
            continue

        # Tile PBF
        pbf = build_parking_tile(raw)
        if pbf:
            tile_path.write_bytes(pbf)
            print(f"ok ({len(pbf)} bytes)")
            written += 1
        else:
            if tile_path.exists():
                tile_path.unlink()
            print("empty (no drivable roads)")
            skipped += 1

        # Segment centroids for segments.json
        segs = extract_segments(raw, z, x, y)
        all_segments.extend(segs)

    # Deduplicate and write segments.json
    unique_segments = deduplicate_segments(all_segments)
    SEGMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "segments": unique_segments,
    }
    SEGMENTS_FILE.write_text(json.dumps(payload, separators=(",", ":")))

    print(
        f"\nDone: {written} tiles written, {skipped} empty, {failed} failed"
    )
    print(
        f"Segments: {len(all_segments)} raw -> {len(unique_segments)} after dedup"
    )
    print(f"Tile directory:  {TILE_DIR.resolve()}")
    print(f"Segments file:   {SEGMENTS_FILE.resolve()}")


if __name__ == "__main__":
    main()
