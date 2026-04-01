"""
Smoke tests for the Predictive Parking FastAPI application.

Validates that key endpoints respond with correct HTTP status codes
without requiring a live database or external services.

External Dependencies: pytest, httpx (via TestClient).
State: None — all external I/O is mocked via conftest fixtures.
"""

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# POST /api/v1/event
# ---------------------------------------------------------------------------


def test_post_event_returns_202(client: TestClient, valid_token: str) -> None:
    """POST /api/v1/parking/event with valid JWT and payload returns HTTP 202."""
    response = client.post(
        "/api/v1/parking/event",
        json={"lat": 51.536, "lon": -0.142, "event_type": "PARKED"},
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


def test_post_event_without_token_returns_401(client: TestClient) -> None:
    """POST /api/v1/parking/event without Authorization header returns HTTP 401."""
    response = client.post(
        "/api/v1/parking/event",
        json={"lat": 51.536, "lon": -0.142, "event_type": "PARKED"},
    )
    assert response.status_code == 401


def test_post_event_invalid_event_type_returns_422(
    client: TestClient, valid_token: str
) -> None:
    """POST /api/v1/parking/event with an invalid event_type returns HTTP 422."""
    response = client.post(
        "/api/v1/parking/event",
        json={"lat": 51.536, "lon": -0.142, "event_type": "UNKNOWN"},
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Sentry debug endpoint
# ---------------------------------------------------------------------------


def test_sentry_debug_returns_500(client: TestClient) -> None:
    """GET /sentry-debug triggers a ZeroDivisionError and returns HTTP 500."""
    response = client.get("/sentry-debug")
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/v1/auth/token — rate limiting
# ---------------------------------------------------------------------------


def test_auth_token_returns_200(client: TestClient) -> None:
    """POST /api/v1/auth/token with valid form data returns HTTP 200."""
    response = client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_auth_token_empty_credentials_returns_422(client: TestClient) -> None:
    """POST /api/v1/auth/token with empty credentials returns HTTP 422."""
    response = client.post(
        "/api/v1/auth/token",
        data={"username": "", "password": ""},
    )
    assert response.status_code == 422


def test_auth_token_rate_limit_after_10_requests(client: TestClient) -> None:
    """POST /api/v1/auth/token returns 429 after 10 requests in one hour."""
    for _ in range(10):
        response = client.post(
            "/api/v1/auth/token",
            data={"username": "testuser", "password": "testpass"},
        )
        assert response.status_code == 200
    response = client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    )
    assert response.status_code == 429


# ---------------------------------------------------------------------------
# GET /api/v1/parking/best_nearby
# ---------------------------------------------------------------------------


def test_best_nearby_returns_200(client: TestClient) -> None:
    """GET /api/v1/parking/best_nearby returns HTTP 200 with recommendations."""
    response = client.get(
        "/api/v1/parking/best_nearby",
        params={"lat": 51.540, "lon": -0.142, "limit": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert "recommendations" in body
    assert "generated_at" in body


def test_best_nearby_limit_exceeded_returns_422(client: TestClient) -> None:
    """GET /api/v1/parking/best_nearby with limit > 20 returns HTTP 422."""
    response = client.get(
        "/api/v1/parking/best_nearby",
        params={"lat": 51.540, "lon": -0.142, "limit": 21},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/health
# ---------------------------------------------------------------------------


def test_health_returns_200(client: TestClient) -> None:
    """GET /api/v1/health returns HTTP 200 with health data."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert "db_connected" in body
    assert "redis_connected" in body
    assert "segment_count" in body
    assert "bay_count" in body
    assert "cpz_count" in body
    assert "enforcement_events_30d" in body
    assert "last_ingestion" in body


# ---------------------------------------------------------------------------
# PH6-060 — Spatial rate limiting on /probability and /best_nearby
# ---------------------------------------------------------------------------


def test_probability_rate_limit_after_60_requests(client: TestClient) -> None:
    """GET /api/v1/parking/probability returns 429 after 60 requests in one minute."""
    for _ in range(60):
        response = client.get(
            "/api/v1/parking/probability",
            params={"lat": 51.540, "lon": -0.142, "radius": 300},
        )
        assert response.status_code == 200
    response = client.get(
        "/api/v1/parking/probability",
        params={"lat": 51.540, "lon": -0.142, "radius": 300},
    )
    assert response.status_code == 429


def test_best_nearby_rate_limit_after_60_requests(client: TestClient) -> None:
    """GET /api/v1/parking/best_nearby returns 429 after 60 requests in one minute."""
    for _ in range(60):
        response = client.get(
            "/api/v1/parking/best_nearby",
            params={"lat": 51.540, "lon": -0.142, "limit": 3},
        )
        assert response.status_code == 200
    response = client.get(
        "/api/v1/parking/best_nearby",
        params={"lat": 51.540, "lon": -0.142, "limit": 3},
    )
    assert response.status_code == 429
