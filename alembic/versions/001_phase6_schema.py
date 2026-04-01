"""Phase 6 schema migration — create new tables, extend streetsegment, create materialized view

Revision ID: 001_phase6_schema
Revises: 
Create Date: 2025-03-28

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from geoalchemy2 import Geometry


# revision identifiers, used by Alembic.
revision = "001_phase6_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade: create Phase 6 schema objects in exact order."""

    # 1. Extend streetsegment with new columns
    op.add_column("streetsegment", sa.Column("street_name", sa.Text, nullable=True))
    op.add_column("streetsegment", sa.Column("osm_id", sa.BigInteger, nullable=True))
    op.add_column("streetsegment", sa.Column("road_type", sa.Text, nullable=True))
    op.add_column("streetsegment", sa.Column("length_m", sa.Float, nullable=True))

    # 2. Create indexes on streetsegment
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_streetsegment_osm_id ON streetsegment(osm_id) WHERE osm_id IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_streetsegment_geom ON streetsegment USING GIST(geom)")

    # 3. Create parking_bay table
    op.create_table(
        "parking_bay",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("streetsegment.id"), nullable=True),
        sa.Column("source_id", sa.Text, nullable=True),
        sa.Column("geom", Geometry("POINT", srid=4326), nullable=True),
        sa.Column("bay_length", sa.Float, nullable=True),
        sa.Column("spaces_est", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("restriction_type", sa.Text, nullable=True),
        sa.Column("restriction_hours", sa.Text, nullable=True),
        sa.Column("days_active", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("geom_hash", sa.Text, sa.Computed("MD5(ST_AsText(geom))", persisted=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        schema=None,
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_parking_bay_source ON parking_bay(source, source_id) WHERE source_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_parking_bay_geom_hash ON parking_bay(geom_hash) WHERE source_id IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_parking_bay_segment ON parking_bay(segment_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_parking_bay_geom ON parking_bay USING GIST(geom)")

    # 4. Create cpz_zone table
    op.create_table(
        "cpz_zone",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cpz_code", sa.Text, unique=True, nullable=False),
        sa.Column("zone_name", sa.Text, nullable=True),
        sa.Column("geom", Geometry("MULTIPOLYGON", srid=4326), nullable=True),
        sa.Column("restriction_schedule", postgresql.JSONB, nullable=True),
        sa.Column("days_active", sa.Text, nullable=True),
        sa.Column("permit_required", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("borough", sa.Text, server_default=sa.text("'camden'"), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        schema=None,
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_cpz_zone_geom ON cpz_zone USING GIST(geom)")

    # 5. Create enforcement_event table
    op.create_table(
        "enforcement_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("streetsegment.id"), nullable=True),
        sa.Column("bay_source_ref", sa.Text, nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hour_of_day", sa.SmallInteger, nullable=True),
        sa.Column("day_of_week", sa.SmallInteger, nullable=True),
        sa.Column("violation_type", sa.Text, nullable=True),
        sa.Column("borough", sa.Text, server_default=sa.text("'camden'"), nullable=False),
        sa.Column("source_file", sa.Text, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        schema=None,
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_enforcement_segment ON enforcement_event(segment_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_enforcement_ts ON enforcement_event(event_ts)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_enforcement_hour_dow ON enforcement_event(hour_of_day, day_of_week)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_enforcement_dedup ON enforcement_event(bay_source_ref, borough, event_ts) WHERE bay_source_ref IS NOT NULL")

    # 6. Create ingestion_run table
    op.create_table(
        "ingestion_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_inserted", sa.Integer, nullable=True),
        sa.Column("rows_updated", sa.Integer, nullable=True),
        sa.Column("rows_skipped", sa.Integer, nullable=True),
        sa.Column("status", sa.Text, server_default=sa.text("'running'"), nullable=False),
        sa.Column("error_msg", sa.Text, nullable=True),
        schema=None,
    )

    # 7. Create street_name_alias table
    op.create_table(
        "street_name_alias",
        sa.Column("pcn_name", sa.Text, primary_key=True),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("streetsegment.id"), nullable=True),
        schema=None,
    )

    # 8. Create enforcement_density_mv materialized view
    op.execute("""
        CREATE MATERIALIZED VIEW enforcement_density_mv AS
        SELECT segment_id, hour_of_day, day_of_week, COUNT(*) AS pcn_count
        FROM enforcement_event
        WHERE event_ts > NOW() - INTERVAL '30 days'
        GROUP BY segment_id, hour_of_day, day_of_week
        WITH DATA
    """)
    op.execute("CREATE INDEX ON enforcement_density_mv(segment_id, hour_of_day, day_of_week)")


def downgrade() -> None:
    """Downgrade: drop all objects in reverse order."""

    # Drop materialized view first
    op.execute("DROP MATERIALIZED VIEW IF EXISTS enforcement_density_mv")

    # Drop tables in reverse dependency order
    op.drop_table("street_name_alias")
    op.drop_table("ingestion_run")
    op.drop_table("enforcement_event")
    op.drop_table("cpz_zone")
    op.drop_table("parking_bay")

    # Drop streetsegment indexes
    op.execute("DROP INDEX IF EXISTS idx_streetsegment_osm_id")
    op.execute("DROP INDEX IF EXISTS idx_streetsegment_geom")

    # Drop streetsegment columns
    op.drop_column("streetsegment", "street_name")
    op.drop_column("streetsegment", "osm_id")
    op.drop_column("streetsegment", "road_type")
    op.drop_column("streetsegment", "length_m")
