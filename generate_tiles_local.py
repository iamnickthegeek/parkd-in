"""
Parkd-In tile generator.

Downloads road geometry directly from the Mapbox Streets v8 vector tiles and
re-encodes them with parking-probability colours.  Because the source geometry
IS the same data Mapbox uses to render the base map, the coloured segments are
guaranteed to sit exactly on the roads.

Usage:
    cd predictive_parking
    python generate_tiles_local.py

Output:
    public/tiles/14/<x>/<y>.pbf  (19 tiles covering Camden at zoom 14)
"""

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
ZOOM = 14

# Camden tile grid at zoom 14
TILE_GRID = [(ZOOM, x, y) for x in range(8183, 8189) for y in range(5443, 5448)]

MAPBOX_TOKEN = (
    "pk.eyJ1Ijoibmlja3RoZWdlZWsiLCJhIjoiY21uOWd3dmx2MDd2MDJzcXl0Nno5czdzbSJ9"
    ".736RofO3J5RUSzyLXT69PQ"
)
MAPBOX_TILESET = "mapbox.mapbox-streets-v8"

# Mapbox Streets road class → (hex color, probability %)
# Classes we care about for on-street parking:
PARK_CLASSES: dict[str, tuple[str, int]] = {
    "street":         ("00AA00", 75),   # local residential streets — high availability
    "street_limited": ("FFAA00", 50),   # limited-access local streets
    "service":        ("00AA00", 75),   # service roads, car parks
    "tertiary":       ("FFAA00", 50),   # tertiary roads
    "secondary":      ("FFAA00", 50),   # secondary roads
    "primary":        ("CC0000", 25),   # primary roads — low availability
    "primary_link":   ("CC0000", 25),
    "trunk":          ("CC0000", 15),   # trunk roads — very low
}


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
# Process
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

    # Encode without quantize_bounds: coordinates are already in 0-4096 pixel
    # space from the Mapbox tile, so no re-projection is needed.
    return mapbox_vector_tile.encode(
        [{"name": "parking", "features": features}],
        default_options={"extents": 4096},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"Downloading {len(TILE_GRID)} tiles from {MAPBOX_TILESET}...")
    written = skipped = failed = 0

    for z, x, y in TILE_GRID:
        tile_path = TILE_DIR / str(z) / str(x) / f"{y}.pbf"
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {z}/{x}/{y} ... ", end="", flush=True)

        raw = download_tile(z, x, y)
        if raw is None:
            print("FAILED")
            failed += 1
            continue

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

    print(f"\nDone: {written} tiles written, {skipped} empty, {failed} failed")
    print(f"Tile directory: {TILE_DIR.resolve()}")
    print("\nNext steps:")
    print("  1. git add public/tiles/ && git commit && git push")
    print("  2. Vercel auto-deploys — tiles served at /tiles/{z}/{x}/{y}.pbf")


if __name__ == "__main__":
    main()
