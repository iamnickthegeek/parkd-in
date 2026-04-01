"""
Unit tests for the probability engine (app/core/engine.py).

Tests run against the live Supabase database via SessionLocal.
Each test verifies a specific factor function or the composite
calculate_probability function.

External Dependencies: pytest, sqlalchemy, live Supabase connection.
State: Reads from streetsegment, cpz_zone, enforcement_density_mv.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engine import calculate_probability, restriction_factor, time_factor
from app.db.database import SessionLocal
from app.db.models import StreetSegment


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provides a live SessionLocal for testing against Supabase."""
    db = SessionLocal()
    yield db
    db.close()


def test_restriction_factor_returns_zero_during_restriction_hours(
    db_session: Session,
) -> None:
    """PH6-061 — restriction_factor returns 0.0 for a segment in a CPZ during restriction hours."""
    # Find a segment inside a CPZ zone that has a restriction_schedule.
    row = db_session.execute(
        text(
            "SELECT s.id, c.restriction_schedule "
            "FROM streetsegment s "
            "JOIN cpz_zone c ON ST_Within(ST_Centroid(s.geom), c.geom) "
            "WHERE c.restriction_schedule IS NOT NULL "
            "LIMIT 1"
        )
    ).first()

    if row is None:
        pytest.skip("No CPZ restriction data")

    segment_id, schedule = row

    # Parse the first restriction entry to find a datetime within the window.
    entry = schedule[0]
    days_str = entry["days"]
    start_str = entry["start"]
    # Map days string to a weekday index.
    day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    if "-" in days_str:
        start_day, _ = days_str.split("-")
        target_weekday = day_map[start_day]
    elif days_str == "All week":
        target_weekday = 0  # Monday
    else:
        target_weekday = day_map.get(days_str, 0)

    # Build a datetime that falls within the restriction window.
    start_h, _ = map(int, start_str.split(":"))
    test_dt = datetime(2026, 4, 6, start_h + 1, 0, 0, tzinfo=UTC)  # Monday 2026-04-06
    # Adjust to the correct weekday.
    current_weekday = test_dt.weekday()
    day_diff = target_weekday - current_weekday
    test_dt = test_dt.replace(day=test_dt.day + day_diff)

    result = restriction_factor(segment_id, test_dt, db_session)
    assert result == 0.0


def test_restriction_factor_returns_one_outside_restriction_hours(
    db_session: Session,
) -> None:
    """PH6-062 — restriction_factor returns 1.0 outside restriction hours."""
    # Find a segment inside a CPZ zone that has a restriction_schedule.
    row = db_session.execute(
        text(
            "SELECT s.id, c.restriction_schedule "
            "FROM streetsegment s "
            "JOIN cpz_zone c ON ST_Within(ST_Centroid(s.geom), c.geom) "
            "WHERE c.restriction_schedule IS NOT NULL "
            "LIMIT 1"
        )
    ).first()

    if row is None:
        pytest.skip("No CPZ restriction data")

    segment_id, schedule = row

    # Parse the first restriction entry to find a datetime OUTSIDE the window.
    entry = schedule[0]
    days_str = entry["days"]
    end_str = entry["end"]
    # Map days string to a weekday index.
    day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    if "-" in days_str:
        _, end_day = days_str.split("-")
        # Pick the day AFTER the restriction range ends.
        target_weekday = (day_map[end_day] + 1) % 7
    elif days_str == "All week":
        # All week with a time window — pick a time outside the hours.
        end_h, _ = map(int, end_str.split(":"))
        test_dt = datetime(2026, 4, 6, end_h + 2, 0, 0, tzinfo=UTC)
        result = restriction_factor(segment_id, test_dt, db_session)
        assert result == 1.0
        return
    else:
        target_weekday = (day_map.get(days_str, 0) + 1) % 7

    # Build a datetime that falls outside the restriction window.
    end_h, end_m = map(int, end_str.split(":"))
    test_dt = datetime(
        2026, 4, 6, end_h + 2, 0, 0, tzinfo=UTC
    )  # After restriction ends
    # Adjust to the correct weekday.
    current_weekday = test_dt.weekday()
    day_diff = target_weekday - current_weekday
    test_dt = test_dt.replace(day=test_dt.day + day_diff)

    result = restriction_factor(segment_id, test_dt, db_session)
    assert result == 1.0


def test_restriction_factor_returns_one_when_no_cpz_found(
    db_session: Session,
) -> None:
    """PH6-063 — restriction_factor returns 1.0 for a segment not in any CPZ."""
    # Use a segment that is not within any CPZ polygon.
    # Query for a segment whose centroid is not inside any cpz_zone.
    row = db_session.execute(
        text(
            "SELECT s.id FROM streetsegment s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM cpz_zone c "
            "  WHERE ST_Within(ST_Centroid(s.geom), c.geom)"
            ") "
            "LIMIT 1"
        )
    ).scalar()

    if row is None:
        # Fallback: use a random UUID that won't match any segment.
        segment_id = uuid.uuid4()
    else:
        segment_id = row

    test_dt = datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC)  # Monday noon
    result = restriction_factor(segment_id, test_dt, db_session)
    assert result == 1.0


def test_time_factor_normalizes_above_10_pcns_to_zero(db_session: Session) -> None:
    """PH6-064 — time_factor returns 0.0 when pcn_count >= 10."""
    # Use any segment UUID — pcn_count is passed directly, bypassing DB lookup.
    row = db_session.execute(text("SELECT id FROM streetsegment LIMIT 1")).scalar()
    if row is None:
        pytest.skip("No streetsegment data")
    segment_id = row

    # pcn_count=10 should return exactly 0.0.
    result_10 = time_factor(segment_id, hour=0, dow=0, db=db_session, pcn_count=10)
    assert result_10 == 0.0

    # pcn_count=15 should also return 0.0 (clamped).
    result_15 = time_factor(segment_id, hour=0, dow=0, db=db_session, pcn_count=15)
    assert result_15 == 0.0

    # pcn_count=5 should return 0.5 (1.0 - 5/10).
    result_5 = time_factor(segment_id, hour=0, dow=0, db=db_session, pcn_count=5)
    assert result_5 == 0.5


def test_time_factor_returns_07_when_no_enforcement_data(db_session: Session) -> None:
    """PH6-065 — time_factor returns 0.7 for a segment with no enforcement history."""
    # Use a random UUID that won't exist in enforcement_density_mv.
    segment_id = uuid.uuid4()
    result = time_factor(segment_id, hour=0, dow=0, db=db_session)
    assert result == 0.7


def test_calculate_probability_clamped_to_0_1(db_session: Session) -> None:
    """PH6-066 — calculate_probability always returns a value in [0.0, 1.0]."""
    rows = (
        db_session.execute(
            text("SELECT id FROM streetsegment ORDER BY RANDOM() LIMIT 5")
        )
        .scalars()
        .all()
    )

    if not rows:
        pytest.skip("No streetsegment data")

    test_dt = datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC)  # Monday noon

    for segment_id in rows:
        # Load the segment with its bays relationship.
        segment = db_session.get(StreetSegment, segment_id)
        if segment is None:
            continue
        # Pass pcn_count=0 and traffic_speed=30 to bypass DB lookups
        # on tables that may not be populated in the test environment.
        result = calculate_probability(
            segment, test_dt, [], db_session, pcn_count=0, traffic_speed=30.0
        )
        assert (
            0.0 <= result <= 1.0
        ), f"calculate_probability({segment_id}) returned {result}, expected [0.0, 1.0]"


def test_calculate_probability_zero_when_restriction_active(
    db_session: Session,
) -> None:
    """PH6-067 — calculate_probability returns 0.0 when restriction_factor returns 0.0."""
    # Find a segment inside a CPZ zone with a restriction_schedule.
    row = db_session.execute(
        text(
            "SELECT s.id, c.restriction_schedule "
            "FROM streetsegment s "
            "JOIN cpz_zone c ON ST_Within(ST_Centroid(s.geom), c.geom) "
            "WHERE c.restriction_schedule IS NOT NULL "
            "LIMIT 1"
        )
    ).first()

    if row is None:
        pytest.skip("No CPZ restriction data")

    segment_id, schedule = row
    segment = db_session.get(StreetSegment, segment_id)
    if segment is None:
        pytest.skip("Segment not found")

    # Parse the first restriction entry to find a datetime within the window.
    entry = schedule[0]
    days_str = entry["days"]
    start_str = entry["start"]
    day_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    if "-" in days_str:
        start_day, _ = days_str.split("-")
        target_weekday = day_map[start_day]
    elif days_str == "All week":
        target_weekday = 0
    else:
        target_weekday = day_map.get(days_str, 0)

    # Build a datetime that falls within the restriction window.
    start_h, _ = map(int, start_str.split(":"))
    test_dt = datetime(2026, 4, 6, start_h + 1, 0, 0, tzinfo=UTC)
    current_weekday = test_dt.weekday()
    day_diff = target_weekday - current_weekday
    test_dt = test_dt.replace(day=test_dt.day + day_diff)

    # Pass pcn_count=0 and traffic_speed=30 to bypass DB lookups
    # on tables that may not be populated in the test environment.
    result = calculate_probability(
        segment, test_dt, [], db_session, pcn_count=0, traffic_speed=30.0
    )
    assert result == 0.0


# This code satisfies PH6-061. No additional functionality added.

# This code satisfies PH6-062. No additional functionality added.

# This code satisfies PH6-063. No additional functionality added.

# This code satisfies PH6-064. No additional functionality added.

# This code satisfies PH6-065. No additional functionality added.

# This code satisfies PH6-066. No additional functionality added.

# This code satisfies PH6-067. No additional functionality added.
