"""HMAC-SHA256 hourly-rotating admin token.

Only SECRET_KEY is required — no static ADMIN_BEARER_TOKEN env var.
Token valid for current hour and previous hour (boundary safe).

External dependencies: hmac, hashlib, time (stdlib only).
State: none.
"""

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import settings


def create_access_token(
    data: dict[str, Any], expires_delta: timedelta | None = None
) -> str:
    """
    Create a JWT access token.

    Args:
        data (dict): The payload to include in the token.
        expires_delta (timedelta, optional): Custom expiration time. Defaults to None.

    Returns:
        str: Encoded JWT token.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )

    return encoded_jwt


def _token_for_bucket(secret_key: str, offset: int = 0) -> str:
    """Generate an HMAC-SHA256 token for a given hour bucket.

    Args:
        secret_key: The SECRET_KEY used for signing.
        offset: Hour offset from current bucket (0 = current, -1 = previous).

    Returns:
        Hex-encoded HMAC-SHA256 digest string.
    """
    bucket = str(int(time.time()) // 3600 + offset)
    return hmac.new(secret_key.encode(), bucket.encode(), hashlib.sha256).hexdigest()


def generate_admin_token(secret_key: str) -> str:
    """Generate the current hour's admin token.

    Args:
        secret_key: The SECRET_KEY used for signing.

    Returns:
        Hex-encoded HMAC-SHA256 token valid for the current hour.
    """
    return _token_for_bucket(secret_key, 0)


def verify_admin_token(token: str, secret_key: str) -> bool:
    """Verify an admin token against current and previous hour buckets.

    Args:
        token: The token to verify.
        secret_key: The SECRET_KEY used for signing.

    Returns:
        True if token matches current or previous hour bucket, False otherwise.
    """
    return hmac.compare_digest(
        token, _token_for_bucket(secret_key, 0)
    ) or hmac.compare_digest(token, _token_for_bucket(secret_key, -1))
