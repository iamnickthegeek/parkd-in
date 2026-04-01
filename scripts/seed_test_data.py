"""Seed minimal test data into cpz_zone and enforcement_density_mv for PH6-061/062/067 tests.

This script inserts a CPZ zone polygon that covers existing street segments
and a restriction_schedule of Mon-Fri 08:30-18:30 so that the restriction_factor
tests can run against real data.

Run from the predictive_parking/ directory with PYTHONPATH=backend.
"""

import json
import sys
from pathlib import Path

# Ensure we can import from backend/app
backend_path = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import SessionLocal


def seed_cpz_zone(db: Session) -> None:
    """Insert a test CPZ zone covering existing Camden segments."""
    # Check if test data already exists
    existing = db.execute(
        text("SELECT COUNT(*) FROM cpz_zone WHERE cpz_code = :code"),
        {"code": "TEST-CPZ-001"},
    ).scalar()
    if existing > 0:
        print("CPZ zone TEST-CPZ-001 already exists — skipping")
        return

    # Polygon covering central Camden area where our segments are
    schedule = [{"days": "Mon-Fri", "start": "08:30", "end": "18:30"}]

    db.execute(
        text(
            """
            INSERT INTO cpz_zone (
                id, cpz_code, zone_name, geom,
                restriction_schedule, days_active,
                permit_required, borough, source
            ) VALUES (
                gen_random_uuid(),
                :cpz_code, :zone_name,
                ST_GeomFromText(
                    'MULTIPOLYGON(((-0.15 51.53, -0.13 51.53, -0.13 51.54, -0.15 51.54, -0.15 51.53)))',
                    4326
                ),
                CAST(:schedule AS jsonb),
                :days_active, true, 'camden', 'test'
            )
            """
        ),
        {
            "cpz_code": "TEST-CPZ-001",
            "zone_name": "Test Camden Zone",
            "schedule": json.dumps(schedule),
            "days_active": "Mon-Fri",
        },
    )
    db.commit()
    print("Inserted CPZ zone TEST-CPZ-001")

    # Verify that segments are covered
    covered = db.execute(
        text(
            "SELECT COUNT(*) FROM streetsegment s "
            "JOIN cpz_zone c ON ST_Within(ST_Centroid(s.geom), c.geom) "
            "WHERE c.cpz_code = :code"
        ),
        {"code": "TEST-CPZ-001"},
    ).scalar()
    print(f"Segments covered by test CPZ zone: {covered}")


def seed_enforcement_data(db: Session) -> None:
    """Insert minimal enforcement events so enforcement_density_mv has data."""
    count = db.execute(text("SELECT COUNT(*) FROM enforcement_event")).scalar()
    if count > 0:
        print(f"Enforcement events already exist ({count}) — skipping")
        return

    # Get a segment ID that's inside our CPZ zone
    segment_row = db.execute(
        text(
            "SELECT s.id FROM streetsegment s "
            "JOIN cpz_zone c ON ST_Within(ST_Centroid(s.geom), c.geom) "
            "WHERE c.cpz_code = 'TEST-CPZ-001' LIMIT 1"
        )
    ).first()

    if segment_row is None:
        print("No segment found inside CPZ zone — cannot seed enforcement data")
        return

    segment_id = segment_row[0]

    # Insert 15 enforcement events for Monday hour 10 (day_of_week=0)
    # This will produce pcn_count=15 in the materialized view
    from datetime import datetime, timezone, timedelta

    base_time = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)  # Monday
    insert_sql = text(
        """
        INSERT INTO enforcement_event
            (id, segment_id, bay_source_ref, event_ts,
             hour_of_day, day_of_week, violation_type, borough, source_file, ingested_at)
        VALUES (gen_random_uuid(), CAST(:segment_id AS uuid), NULL,
            CAST(:event_ts AS timestamptz),
            10, 0, 'permit_zone', 'camden', 'test', now())
        """
    )
    for i in range(15):
        ts = base_time + timedelta(minutes=i)
        db.execute(insert_sql, {
            "segment_id": str(segment_id),
            "event_ts": ts.isoformat(),
        })
    db.commit()
    print(f"Inserted 15 enforcement events for segment {segment_id}")


def refresh_density_view(db: Session) -> None:
    """Refresh the enforcement_density_mv materialized view."""
    # First, ensure the unique index exists for CONCURRENTLY refresh
    db.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_enforcement_density_mv "
            "ON enforcement_density_mv(segment_id, hour_of_day, day_of_week)"
        )
    )
    db.commit()
    try:
        db.execute(
            text("REFRESH MATERIALIZED VIEW CONCURRENTLY enforcement_density_mv")
        )
        db.commit()
    except Exception as exc:
        print(f"CONCURRENTLY refresh failed ({exc}), trying non-concurrent...")
        db.rollback()
        db.execute(text("REFRESH MATERIALIZED VIEW enforcement_density_mv"))
        db.commit()
    print("Refreshed enforcement_density_mv")

    count = db.execute(text("SELECT COUNT(*) FROM enforcement_density_mv")).scalar()
    print(f"Rows in enforcement_density_mv: {count}")


def main() -> None:
    """Run all seeding operations."""
    db = SessionLocal()
    try:
        seed_cpz_zone(db)
        seed_enforcement_data(db)
        refresh_density_view(db)
        print("Seed complete!")
    finally:
        db.close()


if __name__ == "__main__":
    main()
