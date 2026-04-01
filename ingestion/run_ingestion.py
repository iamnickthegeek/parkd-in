#!/usr/bin/env python3
"""
Ingestion CLI runner.

Provides a command-line interface to run individual ingestion sources or all
sources in dependency order.  Each run writes an ``IngestionRun`` record to
the database for monitoring and debugging.

External Dependencies:
    - All ingestion modules (osm_loader, london_datastore, enforcement_loader, tfl_traffic)
    - app.db.database (SessionLocal)
    - app.db.models (IngestionRun)
State:
    - None (stateless CLI entry point).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure the project root (predictive_parking/) is on sys.path so that
# both ``ingestion.*`` and ``app.*`` imports resolve regardless of whether
# PYTHONPATH is set to ``backend`` or left empty.
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Also ensure the backend/ directory is on sys.path for ``app.*`` imports
# when PYTHONPATH is not already set.
_backend_root = _project_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

# Load .env before any app imports
dotenv_path = _project_root / ".env"
try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path)
except ImportError:
    pass  # dotenv not installed yet — will fail later when app modules are imported

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Dependency-ordered list of ingestion sources.
# Each entry is (source_name, callable_that_runs_the_pipeline).
_SOURCE_ORDER: list[str] = ["osm", "bays", "cpz", "enforcement", "tfl"]


def _run_source(source: str, db: Any) -> tuple[int, int, int]:
    """Run a single ingestion source and return (inserted, updated, skipped).

    Args:
        source: One of 'osm', 'bays', 'cpz', 'tfl', 'enforcement'.
        db: SQLAlchemy Session from caller.

    Returns:
        Tuple of (inserted, updated, skipped) counts.

    Raises:
        Exception: Re-raises any unhandled error from the ingestion pipeline.
    """
    if source == "osm":
        from ingestion.osm_loader import (
            fetch_camden_graph,
            segment_edges,
            upsert_segments,
        )

        gdf = fetch_camden_graph()
        segs = segment_edges(gdf)
        inserted, updated = upsert_segments(segs, db)
        return (inserted, updated, 0)

    if source == "bays":
        from ingestion.london_datastore import (
            download_parking_bays,
            normalize_bays,
            upsert_bays,
        )

        gdf = download_parking_bays()
        clean = normalize_bays(gdf)
        inserted, updated = upsert_bays(clean, db)
        return (inserted, updated, 0)

    if source == "cpz":
        from ingestion.london_datastore import (
            download_cpz,
            normalize_cpz,
            upsert_cpz,
        )

        gdf = download_cpz()
        clean = normalize_cpz(gdf)
        count = upsert_cpz(clean, db)
        return (count, 0, 0)

    if source == "enforcement":
        from ingestion.enforcement_loader import (
            download_pcn_csv,
            geocode_pcn_to_segments,
            normalize_pcn,
            refresh_enforcement_density,
            upsert_enforcement,
        )

        df = download_pcn_csv()
        df = normalize_pcn(df)
        df = geocode_pcn_to_segments(df, db)
        count = upsert_enforcement(df, db)
        refresh_enforcement_density(db)
        return (count, 0, 0)

    if source == "tfl":
        from app.core.config import settings

        from ingestion.tfl_traffic import (
            fetch_camden_traffic,
            map_roads_to_segments,
            upsert_traffic,
        )

        records = fetch_camden_traffic(settings.TFL_API_KEY)
        pairs = map_roads_to_segments(records, db)
        count = upsert_traffic(pairs, db)
        return (count, 0, 0)

    logger.error("Unknown source: %s", source)
    return (0, 0, 0)


def _write_ingestion_run(
    db: Any,
    source: str,
    started_at: datetime,
    status: str,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
    error_msg: str | None = None,
) -> None:
    """Write an IngestionRun record to the database.

    Args:
        db: SQLAlchemy Session from caller.
        source: Source name string.
        started_at: When the run started.
        status: 'success' or 'failed'.
        inserted: Number of rows inserted.
        updated: Number of rows updated.
        skipped: Number of rows skipped.
        error_msg: Error message if failed, None otherwise.
    """
    from sqlalchemy import text

    # Rollback any failed transaction before writing the IngestionRun record.
    # Without this the DB session is in an aborted state and all further
    # SQL will fail with InFailedSqlTransaction.
    try:
        db.rollback()
    except Exception:
        pass  # session may already be closed — safe to ignore

    db.execute(
        text("""
            INSERT INTO ingestion_run
                (id, source, started_at, finished_at, rows_inserted,
                 rows_updated, rows_skipped, status, error_msg)
            VALUES
                (:id, :source, :started_at, :finished_at, :inserted,
                 :updated, :skipped, :status, :error_msg)
            """),
        {
            "id": str(uuid.uuid4()),
            "source": source,
            "started_at": started_at,
            "finished_at": datetime.now(UTC),
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "status": status,
            "error_msg": error_msg,
        },
    )
    db.commit()


def _print_summary(results: list[dict[str, Any]]) -> None:
    """Print a formatted summary table of all ingestion source results.

    Args:
        results: List of dicts with keys: source, status, inserted, updated, duration.
    """
    header = f"{'Source':<14} {'Status':<10} {'Inserted':<10} {'Updated':<10} {'Duration':<10}"
    separator = "-" * 54
    print()
    print(header)
    print(separator)
    for r in results:
        print(
            f"{r['source']:<14} {r['status']:<10} {r['inserted']:<10} "
            f"{r['updated']:<10} {r['duration']:.1f}s"
        )
    print()


def main() -> int:
    """Parse CLI arguments and run the requested ingestion source(s).

    Returns:
        0 on success, 1 on any failure.
    """
    parser = argparse.ArgumentParser(description="Run data ingestion sources.")
    parser.add_argument(
        "--source",
        choices=["osm", "bays", "cpz", "tfl", "enforcement", "all"],
        required=True,
        help="Ingestion source to run, or 'all' for all sources.",
    )
    args = parser.parse_args()

    logger.info("Starting ingestion: source=%s", args.source)

    from app.db.database import SessionLocal

    if args.source != "all":
        # Single source mode — run directly, no IngestionRun record
        # (individual source blocks already log their results).
        try:
            db = SessionLocal()
            try:
                inserted, updated, skipped = _run_source(args.source, db)
                logger.info(
                    "%s ingestion complete: inserted=%d, updated=%d, skipped=%d",
                    args.source.title(),
                    inserted,
                    updated,
                    skipped,
                )
            finally:
                db.close()
        except Exception as e:
            logger.exception("Ingestion failed: source=%s, error=%s", args.source, e)
            return 1

        logger.info("Ingestion completed successfully: source=%s", args.source)
        return 0

    # --- 'all' source mode: run in dependency order with IngestionRun records
    results: list[dict[str, Any]] = []
    any_failed = False

    for source in _SOURCE_ORDER:
        logger.info("Running source: %s", source)
        started_at = datetime.now(UTC)
        t0 = time.monotonic()

        db = SessionLocal()
        try:
            inserted, updated, skipped = _run_source(source, db)
            duration = time.monotonic() - t0
            _write_ingestion_run(
                db, source, started_at, "success", inserted, updated, skipped
            )
            results.append(
                {
                    "source": source,
                    "status": "success",
                    "inserted": inserted,
                    "updated": updated,
                    "duration": duration,
                }
            )
            logger.info(
                "%s: success — inserted=%d, updated=%d, skipped=%d (%.1fs)",
                source,
                inserted,
                updated,
                skipped,
                duration,
            )
        except Exception as e:
            duration = time.monotonic() - t0
            _write_ingestion_run(db, source, started_at, "failed", error_msg=str(e))
            results.append(
                {
                    "source": source,
                    "status": "failed",
                    "inserted": 0,
                    "updated": 0,
                    "duration": duration,
                }
            )
            logger.exception("%s: failed after %.1fs — %s", source, duration, e)
            any_failed = True
        finally:
            db.close()

    _print_summary(results)

    if any_failed:
        logger.error("One or more ingestion sources failed.")
        return 1

    logger.info("All sources completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
