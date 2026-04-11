"""
This module manages PostgreSQL database connections via SQLAlchemy.
It exports the engine, a session factory, and a FastAPI dependency provider.
Relies on: sqlalchemy, psycopg2-binary.
"""

# CRITICAL: Patch psycopg2 hstore BEFORE any SQLAlchemy imports.
# SQLAlchemy's psycopg2 dialect calls HstoreAdapter.get_oids() on every new
# connection, which queries pg_type and causes connection drops over PgBouncer.
import psycopg2.extras

_original_get_oids = psycopg2.extras.HstoreAdapter.get_oids


@staticmethod
def _patched_get_oids(conn):
    """Return empty OIDs — we don't use hstore and this avoids the pg_type query."""
    return None


psycopg2.extras.HstoreAdapter.get_oids = _patched_get_oids

# Now safe to import SQLAlchemy
from typing import Generator  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402

# Strip ?pgbouncer=true if present — PgBouncer transaction pooling incompatible with some features.
db_url = settings.SUPABASE_DATABASE_URL.split("?")[0]

engine = create_engine(
    db_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={
        "connect_timeout": 10,
        "sslmode": "disable",
    },
)

SessionLocal: sessionmaker[Session] = sessionmaker(
    autoflush=False,
    autocommit=False,
    bind=engine,
)


def get_db() -> Generator[Session, None, None]:
    """
    Dependency that provides a new SQLAlchemy session for each request.

    Yields:
        Generator[Session, None, None]: A generator that yields a database session.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
