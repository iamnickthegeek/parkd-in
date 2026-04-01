"""
Live integration tests for external service connectivity.

Verifies that Upstash Redis and Cloudflare R2 are reachable and
functional using the credentials in predictive_parking/.env.
No mocks — these tests make real network calls.

External Dependencies: upstash-redis, boto3, requests.
State: Each test writes a canary value and cleans it up within the test.
"""

import uuid

import boto3  # type: ignore
import requests
from upstash_redis import Redis as UpstashRedis

from app.core.config import settings

# ---------------------------------------------------------------------------
# Upstash Redis — live lpush / rpop round-trip
# ---------------------------------------------------------------------------

UPSTASH_TEST_KEY = "integration_test_queue"


def test_upstash_lpush_rpop_round_trip() -> None:
    """
    Push a unique canary payload to Upstash and pop it back.

    Verifies that the Upstash REST endpoint is reachable and that
    the queue write path used by create_parking_event works end-to-end.
    """
    client = UpstashRedis(
        url=settings.UPSTASH_REDIS_REST_URL,
        token=settings.UPSTASH_REDIS_REST_TOKEN,
    )

    canary = f"integration-test-{uuid.uuid4()}"
    client.lpush(UPSTASH_TEST_KEY, canary)
    popped: str | list[str] | None = client.rpop(UPSTASH_TEST_KEY)

    assert popped == canary, f"Expected '{canary}', got '{popped}'"


# ---------------------------------------------------------------------------
# Cloudflare R2 — live put_object + public GET round-trip
# ---------------------------------------------------------------------------

R2_TEST_KEY = "integration-test/canary.txt"


def test_r2_upload_and_public_get() -> None:
    """
    Upload a canary text file to R2 and verify it is publicly accessible.

    Verifies that R2 credentials are valid, the bucket is writable, and
    the public CDN URL returns the uploaded content correctly.
    """
    endpoint = settings.R2_ENDPOINT_URL or (
        f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    )
    r2 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )

    canary_body = f"parkd-in integration test {uuid.uuid4()}".encode()

    r2.put_object(
        Bucket=settings.R2_BUCKET_NAME,
        Key=R2_TEST_KEY,
        Body=canary_body,
        ContentType="text/plain",
    )

    public_url = f"{settings.R2_PUBLIC_URL}/{R2_TEST_KEY}"
    response = requests.get(public_url, timeout=10)

    assert (
        response.status_code == 200
    ), f"Expected 200 from R2 public URL, got {response.status_code}"
    assert (
        response.content == canary_body
    ), f"R2 content mismatch: expected {canary_body!r}, got {response.content!r}"

    # Clean up the canary object so the bucket stays tidy.
    r2.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=R2_TEST_KEY)
