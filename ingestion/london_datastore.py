"""
Downloads Camden parking bay and CPZ data from Camden Open Data and London Datastore.
Validates response schema with Pydantic before processing.

External dependencies: Camden Open Data API, London Datastore API.
State: none.
"""

import hashlib
import json
import logging
import re

import geopandas as gpd  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import CPZZone, ParkingBay
from ingestion.base import get_http_session

logger = logging.getLogger(__name__)


class LondonDatastoreBayRow(BaseModel):
    """Pydantic model for validating parking bay data rows."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source_id: str | None = Field(default=None, alias="unique_identifier")
    spaces_est: int = Field(default=1, alias="parking_spaces")
    restriction_type: str | None = Field(default=None, alias="restriction_type")
    restriction_hours: str | None = Field(default=None, alias="times_of_operation")
    days_active: str | None = Field(default=None, alias="times_of_operation")
    bay_length: float | None = Field(default=None, alias="parking_bay_length_metres")


# Camden Open Data Socrata resource ID for parking bays
CAMDEN_BAYS_RESOURCE_ID = "7hiv-3r9k"


def download_parking_bays(borough: str = "camden") -> gpd.GeoDataFrame:
    """Download parking bay data from Camden Open Data Socrata API.

    Fetches GeoJSON with $limit parameter to retrieve all rows (default
    Socrata limit is 1000).  Falls back to London Datastore if Camden
    returns an error.

    Args:
        borough: Borough name to fetch data for (default: camden).

    Returns:
        GeoDataFrame of raw parking bay features.

    Raises:
        ValueError: If >20% of sample rows fail schema validation.
    """
    session = get_http_session()

    # Camden Open Data Socrata GeoJSON endpoint
    camden_url = (
        f"{settings.CAMDEN_DATA_URL}" f"/resource/{CAMDEN_BAYS_RESOURCE_ID}.geojson"
    )
    try:
        resp = session.get(camden_url, params={"$limit": 10000}, timeout=60)
        if resp.status_code == 200:
            gdf = gpd.read_file(resp.content)
            _validate_schema(gdf)
            logger.info("Downloaded %d bays from Camden Open Data", len(gdf))
            return gdf
    except Exception:
        logger.exception("Camden Open Data fetch failed — falling back")

    # Fall back to London Datastore
    ld_url = (
        f"{settings.LONDON_DATASTORE_URL}" "/dataset/parking-bays/resource/download"
    )
    resp = session.get(ld_url, timeout=60)
    resp.raise_for_status()
    gdf = gpd.read_file(resp.content)
    _validate_schema(gdf)
    logger.info("Downloaded %d bays from London Datastore", len(gdf))
    return gdf


def _validate_schema(gdf: gpd.GeoDataFrame) -> None:
    """Validate a sample of rows against the Pydantic schema.

    Args:
        gdf: GeoDataFrame to validate.

    Raises:
        ValueError: If >20% of the first 10 rows fail validation.
    """
    sample = gdf.head(10)
    fail_count = 0
    for _, row in sample.iterrows():
        try:
            LondonDatastoreBayRow.model_validate(row.to_dict())
        except Exception:
            fail_count += 1
    if fail_count > len(sample) * 0.20:
        raise ValueError("Schema mismatch in bay dataset — aborting ingestion")


def _parse_times_op_days(times_str: str | None) -> str | None:
    """Extract days portion from times_of_operation string.

    Args:
        times_str: Raw times_of_operation value (e.g. "mon-sat 08:30-18:30").

    Returns:
        Normalised days string ("Mon-Sat", "Mon-Fri", "All week", etc.).
    """
    if not times_str or pd.isna(times_str):
        return None
    parts = str(times_str).strip().split()
    if not parts:
        return None
    days_part = parts[0].upper()
    # Normalise common patterns
    if days_part in ("MON-FRI", "MONDAY-FRIDAY"):
        return "Mon-Fri"
    if days_part in ("MON-SAT", "MONDAY-SATURDAY"):
        return "Mon-Sat"
    if days_part in ("ALL", "DAILY", "EVERYDAY"):
        return "All week"
    return parts[0].title()


def _parse_times_op_hours(times_str: str | None) -> str | None:
    """Extract hours portion from times_of_operation string.

    Args:
        times_str: Raw times_of_operation value (e.g. "mon-sat 08:30-18:30").

    Returns:
        Time range string ("08:30-18:30") or None.
    """
    if not times_str or pd.isna(times_str):
        return None
    parts = str(times_str).strip().split()
    if len(parts) < 2:
        return None
    return parts[1]


# Restriction type mapping from Camden Open Data to canonical values
RESTRICTION_TYPE_MAP: dict[str, str] = {
    "permit": "permit",
    "paid-for": "pay",
    "pay": "pay",
    "loading": "loading",
    "disabled": "disabled",
    "free": "free",
    "suspension": "loading",
}


def normalize_bays(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalise raw parking bay GeoDataFrame for database ingestion.

    Maps Camden Socrata column names to standardised names, normalises
    restriction types, estimates spaces, reprojects geometry, and drops
    invalid rows.

    Args:
        gdf: Raw GeoDataFrame from download_parking_bays().

    Returns:
        Cleaned GeoDataFrame with standardised columns in EPSG:4326.
    """
    df = gdf.copy()

    # Map Camden Socrata column names to standard names
    rename_map = {
        "unique_identifier": "source_id",
        "parking_spaces": "spaces_est",
        "restriction_type": "restriction_type",
        "times_of_operation": "restriction_hours",
        "parking_bay_length_metres": "bay_length",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # Normalise restriction_type to canonical values
    if "restriction_type" in df.columns:
        df["restriction_type"] = df["restriction_type"].map(
            lambda x: (
                RESTRICTION_TYPE_MAP.get(str(x).lower(), str(x).lower())
                if pd.notna(x)
                else "free"
            )
        )
        df["restriction_type"] = df["restriction_type"].fillna("free")
    else:
        df["restriction_type"] = "free"

    # Parse times_of_operation into restriction_hours and days_active
    if "restriction_hours" in df.columns:
        df["days_active"] = df["restriction_hours"].apply(_parse_times_op_days)
        df["restriction_hours"] = df["restriction_hours"].apply(_parse_times_op_hours)
    else:
        df["restriction_hours"] = None
        df["days_active"] = None

    # Estimate spaces from bay length if missing or zero (6m per space)
    if "spaces_est" not in df.columns or df["spaces_est"].isna().all():
        df["spaces_est"] = 1
    if "bay_length" in df.columns:
        df["bay_length"] = pd.to_numeric(df["bay_length"], errors="coerce")
        mask = (df["spaces_est"].isna()) | (df["spaces_est"] == 0)
        df.loc[mask, "spaces_est"] = (df.loc[mask, "bay_length"] / 6.0).apply(
            lambda x: max(1, int(x)) if pd.notna(x) else 1
        )
    df["spaces_est"] = df["spaces_est"].fillna(1).astype(int)

    # Ensure required columns exist
    for col in ["source_id", "restriction_hours", "days_active", "bay_length"]:
        if col not in df.columns:
            df[col] = None

    # Reproject to EPSG:4326 if needed
    if df.crs is not None and df.crs.to_epsg() != 4326:
        df = df.to_crs(epsg=4326)
    elif df.crs is None:
        df = df.set_crs(epsg=4326)

    # Drop rows with null geometry
    df = df.dropna(subset=["geometry"])

    # Add source column and select final columns
    df["source"] = "camden_open_data"
    keep_cols = [
        "source_id",
        "spaces_est",
        "restriction_type",
        "restriction_hours",
        "days_active",
        "bay_length",
        "geometry",
        "source",
    ]
    return gpd.GeoDataFrame(
        df[[c for c in keep_cols if c in df.columns]], crs="EPSG:4326"
    )


def _compute_geom_hash(wkt: str) -> str:
    """Compute MD5 hash of a WKT geometry string.

    Args:
        wkt: WKT geometry string.

    Returns:
        MD5 hex digest of the WKT string.
    """
    return hashlib.md5(wkt.encode("utf-8")).hexdigest()


def upsert_bays(gdf: gpd.GeoDataFrame, db: Session) -> tuple[int, int]:
    """Upsert parking bays into the database with spatial join.

    For each bay, finds the nearest street segment within 30m via
    a KNN query using the GiST index. Upserts on composite key
    (source, source_id), falling back to geom_hash for rows without
    a source_id.

    Args:
        gdf: Normalised GeoDataFrame from normalize_bays().
        db: SQLAlchemy Session from caller (do not create a new engine).

    Returns:
        tuple of (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0
    skipped = 0
    batch_size = 200
    rows = list(gdf.itertuples(index=False))
    total = len(rows)

    for batch_start in range(0, total, batch_size):
        batch = rows[batch_start : batch_start + batch_size]
        upsert_values = []

        for row in batch:
            lon = row.geometry.x
            lat = row.geometry.y
            wkt = row.geometry.wkt

            # Spatial join: find nearest segment within 30m
            result = db.execute(
                text("""
                    SELECT id FROM streetsegment
                    WHERE ST_DWithin(
                        geom,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                        0.0003
                    )
                    ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                    LIMIT 1
                    """),
                {"lon": lon, "lat": lat},
            ).fetchone()

            if result is None:
                skipped += 1
                continue

            segment_id = result[0]

            upsert_values.append(
                {
                    "segment_id": segment_id,
                    "source_id": row.source_id if hasattr(row, "source_id") else None,
                    "geom": f"SRID=4326;{wkt}",
                    "bay_length": (
                        row.bay_length if hasattr(row, "bay_length") else None
                    ),
                    "spaces_est": row.spaces_est if hasattr(row, "spaces_est") else 1,
                    "restriction_type": (
                        row.restriction_type
                        if hasattr(row, "restriction_type")
                        else None
                    ),
                    "restriction_hours": (
                        row.restriction_hours
                        if hasattr(row, "restriction_hours")
                        else None
                    ),
                    "days_active": (
                        row.days_active if hasattr(row, "days_active") else None
                    ),
                    "source": "camden_open_data",
                }
            )

        if not upsert_values:
            continue

        # Build upsert statement with composite dedup key (source, source_id).
        # geom_hash is a GENERATED ALWAYS AS STORED column — do not insert/update.
        # Use on_conflict_do_nothing since the unique index is partial and cannot
        # be referenced by name in ON CONFLICT without a proper constraint.
        stmt = insert(ParkingBay).values(upsert_values)
        stmt = stmt.on_conflict_do_nothing()
        db.execute(stmt)
        db.commit()
        inserted += len(upsert_values)
        inserted = inserted  # accumulated above

    match_rate = (total - skipped) / total if total > 0 else 0
    if match_rate < 0.70:
        logger.warning(
            "Bay-segment match rate is %.1f%% (< 70%% threshold). %d of %d rows matched.",
            match_rate * 100,
            total - skipped,
            total,
        )

    logger.info(
        "Bay upsert complete: %d inserted, %d updated, %d skipped (no segment within 30m)",
        inserted,
        updated,
        skipped,
    )
    return (inserted, updated)


def download_cpz(borough: str = "camden") -> gpd.GeoDataFrame:
    """Download CPZ (Controlled Parking Zone) boundary GeoJSON.

    Fetches Camden CPZ boundary GeoJSON from the Camden Open Data portal.
    Validates minimum required columns: CPZ_CODE (or equivalent) and geometry.

    Args:
        borough: Borough name to fetch data for (default: camden).

    Returns:
        GeoDataFrame of raw CPZ zone features.

    Raises:
        ValueError: If required columns are missing from the response.
    """
    session = get_http_session()

    # Camden Open Data Socrata API for CPZ zones (GeoJSON format)
    cpz_url = "https://opendata.camden.gov.uk" "/resource/vf6e-iymu.geojson"
    try:
        resp = session.get(cpz_url, timeout=60)
        if resp.status_code == 200:
            gdf = gpd.read_file(resp.content)
            _validate_cpz_columns(gdf)
            logger.info("Downloaded %d CPZ zones from Camden Open Data", len(gdf))
            return gdf
    except Exception:
        logger.exception("Camden CPZ fetch failed")

    # Last resort: return empty GeoDataFrame
    logger.warning("No CPZ data source available — returning empty GeoDataFrame")
    return gpd.GeoDataFrame(columns=["cpz_code", "geometry"], crs="EPSG:4326")


def _validate_cpz_columns(gdf: gpd.GeoDataFrame) -> None:
    """Validate that CPZ GeoDataFrame has required columns.

    Args:
        gdf: GeoDataFrame to validate.

    Raises:
        ValueError: If required CPZ code column is missing.
    """
    has_code = any(
        col.upper()
        in (
            "CPZ_CODE",
            "CPZ",
            "ZONE_CODE",
            "ZONE",
            "CONTROLLED_PARKING_ZONE_CODE",
            "IDENTIFIER",
        )
        for col in gdf.columns
    )
    if not has_code:
        raise ValueError(
            "CPZ dataset missing required code column — aborting ingestion"
        )


def _parse_cpz_days(days_str: str | None) -> str | None:
    """Normalise a CPZ days string to standard form.

    Args:
        days_str: Raw days string from CPZ data (e.g. "Mon-Fri", "Mon-Sat").

    Returns:
        Normalised days string: "Mon-Fri", "Mon-Sat", "All week", "Sat", etc.
        Returns None if input is None or unrecognisable.
    """
    if days_str is None or pd.isna(days_str):
        return None

    days_upper = str(days_str).strip().upper()

    # Common patterns
    if days_upper in ("ALL WEEK", "EVERY DAY", "DAILY"):
        return "All week"
    if days_upper in ("MON-FRI", "MONDAY-FRIDAY", "WEEKDAYS"):
        return "Mon-Fri"
    if days_upper in ("MON-SAT", "MONDAY-SATURDAY"):
        return "Mon-Sat"
    if days_upper in ("SAT", "SATURDAY"):
        return "Sat"
    if days_upper in ("SUN", "SUNDAY"):
        return "Sun"

    # Try to preserve original if it looks like a valid range
    if "-" in days_upper:
        return str(days_str).strip()

    return str(days_str).strip()


def _parse_cpz_time(time_str: str | None) -> str | None:
    """Normalise a CPZ time string to HH:MM format.

    Args:
        time_str: Raw time string (e.g. "08:30", "8:30", "0830", "At any time").

    Returns:
        Normalised time string in "HH:MM" format, or None if unparseable.
    """
    if time_str is None or pd.isna(time_str):
        return None

    time_upper = str(time_str).strip().upper()

    # Special case: "At any time" means all day
    if "ANY TIME" in time_upper or "AT ANY TIME" in time_upper:
        return None  # Caller handles this as all-day restriction

    # Remove spaces and common separators
    cleaned = time_upper.replace(" ", "").replace(".", "")

    # Try HH:MM or H:MM pattern
    match = re.match(r"^(\d{1,2}):(\d{2})$", cleaned)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    # Try HHMM pattern (e.g. "0830")
    match = re.match(r"^(\d{4})$", cleaned)
    if match:
        hour, minute = int(cleaned[:2]), int(cleaned[2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    # Try HMM pattern (e.g. "830")
    match = re.match(r"^(\d{3})$", cleaned)
    if match:
        hour, minute = int(cleaned[0]), int(cleaned[1:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    return None


def _build_restriction_schedule(row: pd.Series) -> list[dict[str, str]] | None:
    """Build a restriction_schedule list from a CPZ row.

    Args:
        row: A pandas Series representing one CPZ zone row.

    Returns:
        List of restriction dicts in the format:
        [{"days": "Mon-Fri", "start": "08:30", "end": "18:30"}],
        or None if no restriction data.
    """
    restriction_hrs = row.get("RESTRICTION_HRS") or row.get("restriction_hours")
    days = row.get("DAYS") or row.get("days_active") or row.get("days")

    # Handle "At any time" special case
    if restriction_hrs is not None and pd.notna(restriction_hrs):
        hrs_str = str(restriction_hrs).strip()
        if "any time" in hrs_str.lower():
            return [{"days": "All week", "start": "00:00", "end": "23:59"}]

    # Parse time range if present (e.g. "08:30-18:30")
    start_time = None
    end_time = None
    if restriction_hrs is not None and pd.notna(restriction_hrs):
        hrs_str = str(restriction_hrs).strip()
        time_range = re.match(
            r"(\d{1,2}:?\d{0,2})\s*[-–to]+\s*(\d{1,2}:?\d{0,2})", hrs_str
        )
        if time_range:
            start_time = _parse_cpz_time(time_range.group(1))
            end_time = _parse_cpz_time(time_range.group(2))

    normalised_days = _parse_cpz_days(days)

    # If we have both times and days, build the schedule entry
    if start_time and end_time:
        schedule_entry: dict[str, str] = {
            "start": start_time,
            "end": end_time,
        }
        if normalised_days:
            schedule_entry["days"] = normalised_days
        return [schedule_entry]

    # If we only have days but no times, return None (no time restriction)
    if normalised_days and not start_time:
        return None

    return None


def normalize_cpz(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalise raw CPZ GeoDataFrame for database ingestion.

    Parses restriction fields into a restriction_schedule JSONB list format.
    Reprojects geometry to EPSG:4326.

    Args:
        gdf: Raw GeoDataFrame from download_cpz().

    Returns:
        Cleaned GeoDataFrame with columns: cpz_code, zone_name,
        restriction_schedule (list or None), days_active, permit_required,
        borough, source, geometry (MultiPolygon, EPSG:4326).
    """
    df = gdf.copy()

    # Identify the CPZ code column (try various names)
    code_col = None
    for candidate in [
        "CPZ_CODE",
        "cpz_code",
        "CPZ",
        "ZONE_CODE",
        "ZONE",
        "zone_id",
        "controlled_parking_zone_code",
        "CONTROLLED_PARKING_ZONE_CODE",
    ]:
        if candidate in df.columns:
            code_col = candidate
            break

    if code_col is None:
        # Generate synthetic codes from index
        df["cpz_code"] = [f"CPZ_{i:03d}" for i in range(len(df))]
    else:
        df = df.rename(columns={code_col: "cpz_code"})

    # Identify zone name column
    name_col = None
    for candidate in [
        "ZONE_NAME",
        "zone_name",
        "NAME",
        "name",
        "DESCRIPTION",
        "description",
    ]:
        if candidate in df.columns:
            name_col = candidate
            break

    if name_col and name_col != "zone_name":
        df = df.rename(columns={name_col: "zone_name"})
    elif name_col is None:
        df["zone_name"] = None

    # Build restriction_schedule for each row
    df["restriction_schedule"] = df.apply(_build_restriction_schedule, axis=1)

    # Normalise days_active
    days_col = None
    for candidate in ["DAYS", "days_active", "days", "DAYS_ACTIVE"]:
        if candidate in df.columns:
            days_col = candidate
            break

    if days_col and days_col != "days_active":
        df = df.rename(columns={days_col: "days_active"})
    elif days_col is None:
        df["days_active"] = None

    # Determine permit_required from restriction type
    permit_col = None
    for candidate in ["PERMIT_REQUIRED", "permit_required", "PERMIT", "permit"]:
        if candidate in df.columns:
            permit_col = candidate
            break

    if permit_col:
        df["permit_required"] = df[permit_col].map(
            lambda x: (
                str(x).upper() in ("TRUE", "YES", "Y", "1") if pd.notna(x) else True
            )
        )
    else:
        df["permit_required"] = True  # Default: CPZ zones require permits

    # Add standard columns
    df["borough"] = "camden"
    df["source"] = "camden_open_data"

    # Reproject to EPSG:4326
    if df.crs is not None and df.crs.to_epsg() != 4326:
        df = df.to_crs(epsg=4326)
    elif df.crs is None:
        df = df.set_crs(epsg=4326)

    # Drop rows with null geometry
    df = df.dropna(subset=["geometry"])

    # Select final columns
    keep_cols = [
        "cpz_code",
        "zone_name",
        "restriction_schedule",
        "days_active",
        "permit_required",
        "borough",
        "source",
        "geometry",
    ]
    return gpd.GeoDataFrame(
        df[[c for c in keep_cols if c in df.columns]], crs="EPSG:4326"
    )


def upsert_cpz(gdf: gpd.GeoDataFrame, db: Session) -> int:
    """Upsert CPZ zones into the database and link orphan bays to segments.

    For each CPZ zone row, upserts on cpz_code. After all zones are upserted,
    runs a spatial join to link parking_bays without a segment_id to their
    nearest street segment.

    Args:
        gdf: Normalised GeoDataFrame from normalize_cpz().
        db: SQLAlchemy Session from caller (do not create a new engine).

    Returns:
        Count of CPZ rows inserted or updated.
    """
    count = 0
    rows = list(gdf.itertuples(index=False))

    for row in rows:
        sched = (
            row.restriction_schedule if hasattr(row, "restriction_schedule") else None
        )
        sched_json = json.dumps(sched) if sched is not None else None

        # Convert Polygon to MultiPolygon if needed (DB column expects MultiPolygon)
        geom = row.geometry
        if geom.geom_type == "Polygon":
            from shapely.geometry import MultiPolygon  # type: ignore[import-untyped]

            geom = MultiPolygon([geom])
        geom_wkt = geom.wkt

        stmt = (
            insert(CPZZone)
            .values(
                cpz_code=row.cpz_code,
                zone_name=getattr(row, "zone_name", None),
                geom=f"SRID=4326;{geom_wkt}",
                restriction_schedule=sched_json,
                days_active=getattr(row, "days_active", None),
                permit_required=getattr(row, "permit_required", True),
                borough="camden",
                source="camden_open_data",
            )
            .on_conflict_do_update(
                index_elements=["cpz_code"],
                set_={
                    "zone_name": text("EXCLUDED.zone_name"),
                    "geom": text("EXCLUDED.geom"),
                    "restriction_schedule": text("EXCLUDED.restriction_schedule"),
                    "days_active": text("EXCLUDED.days_active"),
                    "permit_required": text("EXCLUDED.permit_required"),
                    "source": text("EXCLUDED.source"),
                },
            )
        )
        db.execute(stmt)
        count += 1

    # Link orphan bays (segment_id IS NULL) to nearest segment within ~100m
    db.execute(text("""
            UPDATE parking_bay SET segment_id = (
                SELECT s.id FROM streetsegment s
                WHERE ST_DWithin(parking_bay.geom, s.geom, 0.001)
                LIMIT 1
            ) WHERE segment_id IS NULL
        """))
    db.commit()

    linked = db.execute(
        text("SELECT COUNT(*) FROM parking_bay WHERE segment_id IS NOT NULL")
    ).scalar()
    total = db.execute(text("SELECT COUNT(*) FROM parking_bay")).scalar()
    logger.info(
        "CPZ upsert complete: %d zones. Bays linked: %d/%d", count, linked, total
    )
    return count
