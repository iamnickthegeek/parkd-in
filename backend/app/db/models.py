"""
This module defines the SQLAlchemy ORM models and the spatial database schema.
It uses GeoAlchemy2 for PostGIS integration and SQLAlchemy 2.0's Mapped typing.

External Dependencies:
- sqlalchemy
- geoalchemy2
- uuid
- datetime
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Initial base for all SQLAlchemy models."""

    pass


class StreetSegment(Base):
    """
    Representation of a street segment with its spatial geometry.
    Persisted in the 'streetsegment' table.
    """

    __tablename__ = "streetsegment"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    geom: Mapped[Geometry] = mapped_column(Geometry("LINESTRING", srid=4326))
    estimated_spaces: Mapped[int] = mapped_column(Integer, nullable=False)
    street_name: Mapped[str | None]
    osm_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    road_type: Mapped[str | None]
    length_m: Mapped[float | None]
    bays: Mapped[list["ParkingBay"]] = relationship(back_populates="segment")


class TrafficHistory(Base):
    """
    Historical record of speed measurements on segments.
    Persisted in the 'traffichistory' table.
    """

    __tablename__ = "traffichistory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("streetsegment.id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, index=True, default=func.now()
    )
    speed: Mapped[float] = mapped_column(Float, nullable=False)


class ParkingEvent(Base):
    """
    User-reported parking events.
    Persisted in the 'parkingevent' table.
    """

    __tablename__ = "parkingevent"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("streetsegment.id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, index=True, default=func.now()
    )
    event_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # PARKED, LEFT, SEARCHING
    user_id: Mapped[str] = mapped_column(String, nullable=False)


class ParkingBay(Base):
    """
    Individual parking bay location and attributes.
    Persisted in the 'parking_bay' table.
    """

    __tablename__ = "parking_bay"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("streetsegment.id"), nullable=True, index=True
    )
    source_id: Mapped[str | None]
    geom: Mapped[Geometry] = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    bay_length: Mapped[float | None]
    spaces_est: Mapped[int] = mapped_column(default=1)
    restriction_type: Mapped[str | None]
    restriction_hours: Mapped[str | None]
    days_active: Mapped[str | None]
    source: Mapped[str]
    geom_hash: Mapped[str | None] = mapped_column(nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    segment: Mapped["StreetSegment"] = relationship(back_populates="bays")


class CPZZone(Base):
    """
    Controlled Parking Zone boundary and restrictions.
    Persisted in the 'cpz_zone' table.
    """

    __tablename__ = "cpz_zone"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cpz_code: Mapped[str] = mapped_column(unique=True, nullable=False)
    zone_name: Mapped[str | None]
    geom: Mapped[Geometry] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326), nullable=True
    )
    restriction_schedule: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    days_active: Mapped[str | None]
    permit_required: Mapped[bool] = mapped_column(default=False)
    borough: Mapped[str] = mapped_column(default="camden")
    source: Mapped[str]
    ingested_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class EnforcementEvent(Base):
    """
    Enforcement event (PCN - Penalty Charge Notice).
    Persisted in the 'enforcement_event' table.
    """

    __tablename__ = "enforcement_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("streetsegment.id"), nullable=True, index=True
    )
    bay_source_ref: Mapped[str | None]
    event_ts: Mapped[datetime] = mapped_column(index=True)
    hour_of_day: Mapped[int | None]
    day_of_week: Mapped[int | None]
    violation_type: Mapped[str | None]
    borough: Mapped[str] = mapped_column(default="camden")
    source_file: Mapped[str | None]
    ingested_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class IngestionRun(Base):
    """
    Tracks each ingestion pipeline run for monitoring and debugging.
    Persisted in the 'ingestion_run' table.
    """

    __tablename__ = "ingestion_run"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    rows_inserted: Mapped[int | None] = mapped_column(nullable=True)
    rows_updated: Mapped[int | None] = mapped_column(nullable=True)
    rows_skipped: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default="running")
    error_msg: Mapped[str | None] = mapped_column(nullable=True)


class StreetNameAlias(Base):
    """
    Mapping of PCN street names to canonical street segment IDs.
    Persisted in the 'street_name_alias' table.
    """

    __tablename__ = "street_name_alias"

    pcn_name: Mapped[str] = mapped_column(primary_key=True)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("streetsegment.id"), nullable=True
    )
