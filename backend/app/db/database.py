"""
This module manages PostgreSQL database connections via SQLAlchemy.
It exports the engine, a session factory, and a FastAPI dependency provider.
Relies on: sqlalchemy, psycopg2-binary.
"""

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# Create the SQLAlchemy engine using the DSN from settings.
# pool_pre_ping is used to check connection health before use, pool_size is tuned for PgBouncer.
# Strip ?pgbouncer=true if present — it's only for SQLAlchemy's async mode, not for psycopg2 DSN.
db_url = settings.SUPABASE_DATABASE_URL.split("?")[0]
engine = create_engine(
    db_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

# sessionmaker returns a class that maintains database sessions.
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
