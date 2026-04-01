"""Background scheduler for predictive parking worker tasks.

This module initializes and manages a background scheduler using APScheduler.
It triggers periodic queue draining from Upstash, MVT tile generation
uploads to Cloudflare R2, TfL traffic polling, and enforcement data refresh.

External Dependencies:
- apscheduler
- sqlalchemy (via app.db.database)
"""

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]

from app.core.engine import update_prediction_tiles_r2
from app.db.database import SessionLocal
from app.db.models import IngestionRun
from worker.queue_processor import drain_events_queue

logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)


def refresh_tfl_traffic() -> None:
    """Poll TfL API for Camden A-road speeds and upsert into traffichistory.

    Scheduled every 15 minutes. Writes an IngestionRun record on success
    or failure. Exceptions are caught and logged — never re-raised.
    """
    from app.core.config import settings

    from ingestion.tfl_traffic import (
        fetch_camden_traffic,
        map_roads_to_segments,
        upsert_traffic,
    )

    db = SessionLocal()
    run = IngestionRun(
        source="tfl",
        started_at=datetime.now(UTC),
        status="running",
    )
    try:
        records = fetch_camden_traffic(settings.TFL_API_KEY)
        pairs = map_roads_to_segments(records, db)
        count = upsert_traffic(pairs, db)
        run.finished_at = datetime.now(UTC)
        run.status = "success"
        run.rows_inserted = count
        db.add(run)
        db.commit()
        logger.info("TfL traffic refresh: %d records inserted", count)
    except Exception:
        logger.exception("TfL traffic refresh failed")
        db.rollback()
        run.finished_at = datetime.now(UTC)
        run.status = "failed"
        run.error_msg = "TfL traffic refresh failed"
        db.add(run)
        db.commit()
    finally:
        db.close()


def refresh_enforcement_data() -> None:
    """Download, normalise, geocode, and upsert Camden PCN enforcement data.

    Scheduled daily at 06:00. Runs the full enforcement pipeline:
    download → normalize → geocode → upsert → refresh materialized view.
    Writes an IngestionRun record on success or failure. Exceptions are
    caught and logged — never re-raised.
    """
    from ingestion.enforcement_loader import (
        download_pcn_csv,
        geocode_pcn_to_segments,
        normalize_pcn,
        refresh_enforcement_density,
        upsert_enforcement,
    )

    db = SessionLocal()
    run = IngestionRun(
        source="enforcement",
        started_at=datetime.now(UTC),
        status="running",
    )
    try:
        df = download_pcn_csv()
        df = normalize_pcn(df)
        df = geocode_pcn_to_segments(df, db)
        count = upsert_enforcement(df, db)
        refresh_enforcement_density(db)
        run.finished_at = datetime.now(UTC)
        run.status = "success"
        run.rows_inserted = count
        db.add(run)
        db.commit()
        logger.info("Enforcement data refresh: %d records inserted", count)
    except Exception:
        logger.exception("Enforcement data refresh failed")
        db.rollback()
        run.finished_at = datetime.now(UTC)
        run.status = "failed"
        run.error_msg = "Enforcement data refresh failed"
        db.add(run)
        db.commit()
    finally:
        db.close()


# SCA-015 — Wire update_prediction_tiles_r2 into scheduler every 5 minutes.
def refresh_prediction_tiles() -> None:
    """Open a DB session and trigger MVT tile generation + R2 upload.

    Called by the scheduler every 5 minutes.
    """
    from upstash_redis.asyncio import Redis as AsyncRedis

    from app.core.config import settings

    db = SessionLocal()
    redis = AsyncRedis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )
    try:
        asyncio.run(update_prediction_tiles_r2(db, redis))
    finally:
        db.close()
        asyncio.run(redis.close())


def start_scheduler() -> BackgroundScheduler:
    """Initialize and start the background scheduler.

    Configures tile refresh (5 min) and queue drain (1 min) jobs.

    Returns:
        BackgroundScheduler: The started scheduler instance.
    """
    scheduler: BackgroundScheduler = BackgroundScheduler()
    scheduler.add_job(
        refresh_prediction_tiles,
        "interval",
        minutes=5,
        id="refresh_prediction_tiles_job",
        replace_existing=True,
    )
    # SCA-010 — Register drain_events_queue to run every 1 minute.
    scheduler.add_job(
        drain_events_queue,
        "interval",
        minutes=1,
        id="drain_events_queue_job",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_tfl_traffic,
        "interval",
        minutes=15,
        id="tfl_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_enforcement_data,
        "cron",
        hour=6,
        minute=0,
        id="enforcement_refresh",
        replace_existing=True,
    )
    # This code satisfies SCA-010. No additional functionality added.
    # This code satisfies PH6-037. No additional functionality added.
    # This code satisfies PH6-038. No additional functionality added.
    scheduler.start()
    logger.info(
        "Background scheduler started (tile refresh: 5 min, queue drain: 1 min, "
        "TfL refresh: 15 min, enforcement refresh: daily 06:00)."
    )
    return scheduler


# This code satisfies SCA-015. No additional functionality added.
