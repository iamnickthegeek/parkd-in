"""
Test configuration and shared fixtures for the predictive parking backend.

Provides:
- TestClient instance with Upstash and scheduler mocked out.
- A valid JWT bearer token for authenticated endpoint tests.

External Dependencies: pytest, httpx (via starlette TestClient), pyjwt.
State: No persistent state; all mocks are function-scoped.
"""

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token


@pytest.fixture(scope="session")
def valid_token() -> str:
    """
    Return a signed JWT for user 'test_user' valid for 60 minutes.

    Returns:
        str: Encoded JWT access token.
    """
    return create_access_token(
        data={"sub": "test_user"},
        expires_delta=timedelta(minutes=60),
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """
    Yield a FastAPI TestClient with Upstash Redis and APScheduler mocked.

    The scheduler start is no-op'd so no background threads are created.
    The Upstash lpush is no-op'd so no network calls leave the process.
    The Redis incr/expire are mocked for rate limiting tests.

    Yields:
        TestClient: Configured test client for the FastAPI app.
    """
    mock_upstash = MagicMock()
    mock_upstash.lpush.return_value = 1
    mock_upstash.ping.return_value = True
    mock_upstash.get.return_value = None

    # Track incr counts per key for rate limiting tests
    _incr_counts: dict[str, int] = {}

    async def mock_incr(key: str) -> int:
        _incr_counts[key] = _incr_counts.get(key, 0) + 1
        return _incr_counts[key]

    async def mock_expire(key: str, seconds: int) -> bool:
        return True

    mock_upstash.incr.side_effect = mock_incr
    mock_upstash.expire.side_effect = mock_expire

    # Mock database session
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 0
    mock_result.all.return_value = []
    mock_db.execute.return_value = mock_result

    def mock_get_db():
        """Mock generator that yields a fake DB session."""
        yield mock_db

    async def mock_get_redis():
        """Mock async generator that yields a fake Redis client."""
        yield mock_upstash

    with (
        patch("worker.scheduler.start_scheduler", return_value=None),
        patch(
            "app.api.endpoints.parking._get_upstash_client",
            return_value=mock_upstash,
        ),
        patch(
            "app.db.redis_client.get_redis_client",
            return_value=mock_upstash,
        ),
        patch(
            "app.api.endpoints.parking.get_db",
            mock_get_db,
        ),
        patch(
            "app.api.endpoints.health.get_db",
            mock_get_db,
        ),
        patch(
            "app.api.endpoints.health.get_redis",
            mock_get_redis,
        ),
    ):
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
