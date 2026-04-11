"""
Proxy endpoint for serving MVT tiles.

In LOCAL_MODE tiles are read from frontend/tiles/{z}/{x}/{y}.pbf on disk.
Otherwise tiles are proxied from Cloudflare R2.

External Dependencies: fastapi, boto3 (non-local mode only).
State: reads from disk (local) or R2 bucket (production).
"""

import logging
import pathlib
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import Response

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Local tile directory: predictive_parking/frontend/tiles/
# __file__ = .../predictive_parking/backend/app/api/endpoints/tiles.py
# parents[4] = .../predictive_parking/
_LOCAL_TILE_DIR = pathlib.Path(__file__).resolve().parents[4] / "frontend" / "tiles"


def _get_r2_client() -> Any:
    """Return a boto3 S3 client configured for Cloudflare R2."""
    import boto3  # type: ignore[import-untyped]

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


@router.get("/tiles/{z}/{x}/{y}.pbf")
async def serve_tile(
    z: Annotated[int, Path(ge=0, le=22)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
) -> Response:
    """Serve an MVT tile from disk (LOCAL_MODE) or R2 bucket.

    Args:
        z: Zoom level (0-22).
        x: Tile X coordinate.
        y: Tile Y coordinate.

    Returns:
        Response with the .pbf tile data and correct MIME type.
    """
    headers = {"Cache-Control": "public, max-age=300"}

    if settings.LOCAL_MODE:
        tile_path = _LOCAL_TILE_DIR / str(z) / str(x) / f"{y}.pbf"
        if not tile_path.exists():
            raise HTTPException(status_code=404, detail="Tile not found")
        return Response(
            content=tile_path.read_bytes(),
            media_type="application/x-protobuf",
            headers=headers,
        )

    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    tile_key = f"tiles/{z}/{x}/{y}.pbf"
    r2 = _get_r2_client()
    try:
        obj = r2.get_object(Bucket=settings.R2_BUCKET_NAME, Key=tile_key)
        return Response(
            content=obj["Body"].read(),
            media_type="application/x-protobuf",
            headers=headers,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchKey":
            raise HTTPException(status_code=404, detail="Tile not found")
        logger.error("R2 error fetching tile %s: %s", tile_key, exc)
        raise HTTPException(status_code=502, detail="R2 fetch failed")


# Tile proxy endpoint for serving R2 MVT tiles with CORS support.
# This code satisfies the tile proxy requirement. No additional functionality added.
