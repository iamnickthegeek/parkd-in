"""
Redis client configuration and singleton instance using Upstash REST API.
All Redis operations use the Upstash async REST client.

External Dependencies: upstash_redis (with httpx async support).
State: singleton Upstash Redis client.
"""

import logging
from collections.abc import AsyncGenerator

from upstash_redis.asyncio import Redis as AsyncRedis

from app.core.config import settings

logger: logging.Logger = logging.getLogger(__name__)

# Global private instance for the Upstash Redis client.
_instance: AsyncRedis | None = None


def get_redis_client() -> AsyncRedis:
    """
    Ensures a singleton Upstash Redis async client is returned.

    Returns:
        AsyncRedis: A configured Upstash async Redis client.
    """
    global _instance
    if _instance is None:
        logger.info("Initializing Upstash Redis client...")
        _instance = AsyncRedis(
            url=settings.UPSTASH_REDIS_REST_URL,
            token=settings.UPSTASH_REDIS_REST_TOKEN,
        )
    return _instance


async def get_redis() -> AsyncGenerator[AsyncRedis, None]:
    """
    FastAPI dependency yielding the configured Upstash Redis client.

    Yields:
        AsyncGenerator[AsyncRedis, None]: An async generator yielding the client.
    """
    client: AsyncRedis = get_redis_client()
    try:
        yield client
    finally:
        pass
