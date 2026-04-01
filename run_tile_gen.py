"""Run tile generation to populate segment_probs Redis hash."""
import asyncio
from dotenv import load_dotenv

load_dotenv(".env")

from app.db.database import SessionLocal  # noqa: E402
from app.core.engine import update_prediction_tiles_r2  # noqa: E402
from upstash_redis.asyncio import Redis as AsyncRedis  # noqa: E402
from app.core.config import settings  # noqa: E402


async def main() -> None:
    """Run tile generation against live DB and Redis."""
    db = SessionLocal()
    redis = AsyncRedis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )
    try:
        await update_prediction_tiles_r2(db, redis)
        count = len(await redis.hgetall("segment_probs") or {})
        print(f"Segment probs cached: {count}")
        assert count > 1000, f"Expected > 1000 segments, got {count}"
        print("Phase 8 PASS")
    finally:
        db.close()
        await redis.close()


if __name__ == "__main__":
    asyncio.run(main())
