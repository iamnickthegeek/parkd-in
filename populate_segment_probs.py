"""Populate segment_probs Redis hash for smoke testing."""
import asyncio
import json
import logging
from datetime import UTC, datetime

from dotenv import load_dotenv

load_dotenv(".env")

from app.db.database import SessionLocal  # noqa: E402
from app.core.engine import load_segment_batch_data  # noqa: E402
from upstash_redis.asyncio import Redis as AsyncRedis  # noqa: E402
from app.core.config import settings  # noqa: E402

logger = logging.getLogger(__name__)


def _prob_to_color(prob: float) -> int:
    if prob >= 0.7:
        return 0x00AA00
    if prob >= 0.3:
        return 0xFFAA00
    return 0xCC0000


def _calc_prob(row: dict) -> float:
    f_capacity = min(1.0, row["total_spaces"] / 10.0)
    f_time = max(0.0, 1.0 - float(row["pcn_count"]) / 10.0)
    speed = row.get("traffic_speed")
    f_traffic = max(0.5, min(1.0, speed / 50.0)) if speed else 0.7
    raw = f_capacity * 0.20 + f_time * 0.25 + f_traffic * 0.20 + 0.0 * 0.15 + 1.0 * 0.20
    return float(max(0.0, min(1.0, raw)))


async def main() -> None:
    """Load segment data and write to Redis segment_probs hash."""
    db = SessionLocal()
    redis = AsyncRedis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )
    try:
        now = datetime.now(UTC)
        rows = load_segment_batch_data(db, now.hour, now.weekday())
        print(f"Loaded {len(rows)} segments from DB")

        mapping = {}
        for r in rows:
            sid = r["segment_id"]
            prob = _calc_prob(r)
            mapping[sid] = json.dumps({
                "prob": prob,
                "lat": r["lat"],
                "lon": r["lon"],
                "street_name": r["street_name"] or "",
                "bay_count": r["bay_count"],
            })

        if mapping:
            await redis.hset("segment_probs", values=mapping)
            await redis.set("r2_last_tile_upload", now.isoformat())
            print(f"Cached {len(mapping)} segment probabilities in Redis")
        else:
            print("No segment data found")

        # Verify
        count = len(await redis.hgetall("segment_probs") or {})
        print(f"segment_probs count: {count}")
        if count > 0:
            print("Phase 8 PASS")
        else:
            print("WARNING: No data cached")
    finally:
        db.close()
        await redis.close()


if __name__ == "__main__":
    asyncio.run(main())
