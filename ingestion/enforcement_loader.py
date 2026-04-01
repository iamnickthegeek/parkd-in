"""
Camden PCN enforcement event downloader and loader.

Downloads from opendata.camden.gov.uk. Uses Last-Modified header via Redis
key ``pcn_last_modified:{borough}`` to skip unchanged data.

External dependency:
    Camden Open Data (CKAN API).

State: none.
"""

import io
import logging
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.orm import Session

from ingestion.base import get_http_session, normalise_street

logger = logging.getLogger(__name__)

# Camden PCN contravention code to human-readable violation type mapping.
CONTRAVENTION_MAP: dict[str, str] = {
    "06": "permit_zone",
    "01": "no_return",
    "16": "loading",
    "30": "no_stopping",
}

# CKAN resource ID for Camden "Parking Penalties Issued" dataset.
# Verify the actual resource ID from opendata.camden.gov.uk before production use.
PCN_RESOURCE_ID: str = "parking-penalties-issued"


def download_pcn_csv(
    borough: str = "camden",
    redis_client: Any = None,
) -> pd.DataFrame:
    """Download Camden PCN enforcement CSV from the Open Data portal.

    Uses a HEAD request to check the Last-Modified header.  If a Redis
    client is supplied and the header matches the cached value the download
    is skipped and an empty DataFrame is returned.

    Args:
        borough: Borough slug (default ``"camden"``).
        redis_client: Optional Upstash Redis client for cache lookups.

    Returns:
        DataFrame containing raw PCN records, or empty DataFrame on
        cache-hit or download failure.
    """
    url = (
        f"{_camden_data_url()}"
        f"/api/3/action/datastore_search"
        f"?resource_id={PCN_RESOURCE_ID}"
    )

    session = get_http_session()

    # --- HEAD request to obtain Last-Modified --------------------------
    try:
        head = session.head(url, timeout=(10, 30))
    except Exception:
        logger.exception("HEAD request failed for %s", url)
        return pd.DataFrame()

    if head.status_code != 200:
        logger.error("PCN endpoint returned %d — skipping", head.status_code)
        return pd.DataFrame()

    last_modified: str | None = head.headers.get("Last-Modified")

    # --- Redis cache check ---------------------------------------------
    if redis_client is not None and last_modified is not None:
        cache_key = f"pcn_last_modified:{borough}"
        try:
            cached = redis_client.get(cache_key)
            if cached == last_modified:
                logger.info("PCN CSV unchanged — skipping")
                return pd.DataFrame()
        except Exception:
            logger.warning("Redis unavailable — downloading anyway")

    # --- GET the CSV ---------------------------------------------------
    try:
        response = session.get(url, timeout=(10, 120))
    except Exception:
        logger.exception("GET request failed for %s", url)
        return pd.DataFrame()

    if response.status_code != 200:
        logger.error("PCN download returned %d", response.status_code)
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO(response.text))
    logger.info(
        "Downloaded PCN CSV: %d rows, %d columns",
        len(df),
        len(df.columns),
    )

    # --- Update Redis cache --------------------------------------------
    if redis_client is not None and last_modified is not None:
        try:
            redis_client.set(f"pcn_last_modified:{borough}", last_modified)
        except Exception:
            logger.warning("Failed to update Redis cache key")

    return df


def _camden_data_url() -> str:
    """Return the Camden Open Data base URL from settings.

    Deferred import avoids circular dependencies when this module is
    imported from scripts that do not need the full app config.

    Returns:
        Base URL string for the Camden Open Data portal.
    """
    from app.core.config import settings

    return settings.CAMDEN_DATA_URL


def normalize_pcn(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise raw PCN DataFrame into enforcement-event-ready columns.

    Parses date/time, derives hour_of_day and day_of_week, maps
    contravention codes to violation types, and drops rows with missing
    street names.

    Args:
        df: Raw DataFrame from download_pcn_csv().

    Returns:
        Normalised DataFrame with columns: event_ts, hour_of_day,
        day_of_week, violation_type, street, source_file, borough,
        pcn_ref.
    """
    if df.empty:
        logger.info("No PCN records to normalize — skipping")
        return df

    df = df.copy()

    # Parse event_ts from DATE_ISSUED + TIME_ISSUED
    df["event_ts"] = pd.to_datetime(
        df["DATE_ISSUED"].astype(str) + " " + df["TIME_ISSUED"].astype(str),
        errors="coerce",
        utc=True,
    )
    df = df.dropna(subset=["event_ts"])

    df["hour_of_day"] = df["event_ts"].dt.hour.astype("Int64")
    df["day_of_week"] = df["event_ts"].dt.dayofweek.astype("Int64")

    # Map contravention code to violation type
    df["violation_type"] = (
        df["CONTRAVENTION_CODE"].map(CONTRAVENTION_MAP).fillna("other")
    )

    # Rename STREET column and drop rows where street is null
    df = df.rename(columns={"STREET": "street"})
    df = df.dropna(subset=["street"])

    # Select and order output columns
    df["source_file"] = "camden_pcn"
    df["borough"] = "camden"
    df = df.rename(columns={"PCN_REF": "pcn_ref"})

    return df[
        [
            "event_ts",
            "hour_of_day",
            "day_of_week",
            "violation_type",
            "street",
            "source_file",
            "borough",
            "pcn_ref",
        ]
    ]


def geocode_pcn_to_segments(df: pd.DataFrame, db: Session) -> pd.DataFrame:
    """Map PCN street names to street segment IDs via fuzzy matching.

    Checks the ``street_name_alias`` table first, then falls back to
    normalised matching against ``streetsegment.street_name``.  Logs a
    warning if the match rate drops below 60%.

    Args:
        df: Normalised PCN DataFrame with a ``street`` column.
        db: SQLAlchemy Session (caller-managed).

    Returns:
        DataFrame with an added ``segment_id`` column (UUID or None).
    """
    if df.empty:
        return df

    # Load segment names into a dict: normalised_name -> segment_id (str)
    seg_rows = (
        db.execute(
            text(
                "SELECT id::text, street_name FROM streetsegment WHERE street_name IS NOT NULL"
            ),
        )
        .mappings()
        .all()
    )
    seg_dict: dict[str, str] = {
        normalise_street(r["street_name"]): r["id"] for r in seg_rows
    }

    # Load manual aliases: pcn_name (upper) -> segment_id (str)
    alias_rows = (
        db.execute(
            text("SELECT pcn_name, segment_id::text FROM street_name_alias"),
        )
        .mappings()
        .all()
    )
    alias_dict: dict[str, str] = {
        r["pcn_name"].upper(): r["segment_id"] for r in alias_rows
    }

    def _resolve_street(street: str) -> str | None:
        """Resolve a single street name to a segment ID."""
        upper = street.upper()
        if upper in alias_dict:
            return alias_dict[upper]
        norm = normalise_street(street)
        return seg_dict.get(norm)

    street_to_sid: dict[str, str | None] = {}
    for street in df["street"].unique():
        street_to_sid[street] = _resolve_street(street)

    df = df.copy()
    df["segment_id"] = df["street"].map(street_to_sid)

    match_rate = df["segment_id"].notna().mean()
    if match_rate < 0.60:
        unmatched = df[df["segment_id"].isna()]["street"].value_counts().head(10)
        logger.warning(
            "Low PCN match rate (%.1f%%). Top unmatched streets:\n%s",
            match_rate * 100,
            unmatched.to_string(),
        )

    return df


def upsert_enforcement(df: pd.DataFrame, db: Session) -> int:
    """Upsert enforcement events into the database.

    Filters rows with a non-null segment_id, batches inserts, and
    uses ON CONFLICT DO NOTHING for deduplication.

    Args:
        df: Geocoded PCN DataFrame with ``segment_id`` column.
        db: SQLAlchemy Session (caller-managed).

    Returns:
        Number of rows inserted.
    """
    if df.empty:
        logger.info("No enforcement records to upsert — skipping")
        return 0

    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import EnforcementEvent

    total_before = len(df)
    df = df[df["segment_id"].notna()].copy()
    skipped = total_before - len(df)

    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "segment_id": row["segment_id"],
                "bay_source_ref": row.get("pcn_ref"),
                "event_ts": row["event_ts"],
                "hour_of_day": row["hour_of_day"],
                "day_of_week": row["day_of_week"],
                "violation_type": row["violation_type"],
                "borough": row["borough"],
                "source_file": row["source_file"],
            }
        )

    count = 0
    batch_size = 1000
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        stmt = insert(EnforcementEvent).values(batch)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["bay_source_ref", "borough", "event_ts"],
        )
        db.execute(stmt)
        count += len(batch)

    db.commit()
    logger.info("Enforcement events inserted: %d (skipped %d)", count, skipped)
    return count


def refresh_enforcement_density(db: Session) -> None:
    """Refresh the ``enforcement_density_mv`` materialized view.

    Tries the CONCURRENTLY variant first (requires a unique index).
    Falls back to a standard refresh if the unique index is missing.

    Args:
        db: SQLAlchemy Session (caller-managed).
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError

    try:
        db.execute(
            text("REFRESH MATERIALIZED VIEW CONCURRENTLY enforcement_density_mv"),
        )
        db.commit()
        logger.info("enforcement_density_mv refreshed (concurrent)")
    except (ProgrammingError, OperationalError):
        db.rollback()
        try:
            db.execute(text("REFRESH MATERIALIZED VIEW enforcement_density_mv"))
            db.commit()
            logger.warning(
                "enforcement_density_mv refreshed (non-concurrent — "
                "add unique index for concurrent refresh)"
            )
        except (ProgrammingError, OperationalError) as e2:
            db.rollback()
            logger.error("enforcement_density_mv refresh failed entirely: %s", e2)
