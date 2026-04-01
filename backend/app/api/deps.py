"""
API dependencies for the predictive parking application.

This module provides FastAPI dependencies for authentication and data access.
External dependencies: jwt, fastapi, redis (via get_redis).
State: none.
"""

import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import settings
from app.db.redis_client import get_redis

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def verify_token(token: Annotated[str, Depends(oauth2_scheme)]) -> str:
    """
    Verify the JWT token and return the user ID.

    Args:
        token (str): The JWT access token.

    Returns:
        str: The user ID extracted from the token.

    Raises:
        HTTPException: If the token is invalid or expired.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        return user_id
    except jwt.PyJWTError:
        raise credentials_exception


async def check_token_rate_limit(
    request: Request,
    redis_client: Annotated[object, Depends(get_redis)],
) -> None:
    """
    Rate-limit /auth/token to 10 requests per hour per client IP.

    Fails open: if Redis is unavailable, the request is allowed through.

    Args:
        request: The incoming FastAPI request.
        redis_client: Upstash Redis client from the dependency injection.

    Raises:
        HTTPException: 429 if the rate limit is exceeded.
    """
    if request.client is None:
        return
    key = f"ratelimit:token:{request.client.host}"
    try:
        count = int(await redis_client.incr(key) or 0)  # type: ignore[attr-defined]
        if count == 1:
            await redis_client.expire(key, 3600)  # type: ignore[attr-defined]
        if count > 10:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": "3600"},
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("Token rate limit unavailable — allowing request")


async def check_spatial_rate_limit(
    request: Request,
    redis_client: Annotated[object, Depends(get_redis)],
) -> None:
    """
    Rate-limit spatial endpoints to 60 requests per minute per client IP.

    Fails open: if Redis is unavailable, the request is allowed through.

    Args:
        request: The incoming FastAPI request.
        redis_client: Upstash Redis client from the dependency injection.

    Raises:
        HTTPException: 429 if the rate limit is exceeded.
    """
    if request.client is None:
        return
    key = f"ratelimit:spatial:{request.client.host}"
    try:
        count = int(await redis_client.incr(key) or 0)  # type: ignore[attr-defined]
        if count == 1:
            await redis_client.expire(key, 60)  # type: ignore[attr-defined]
        if count > 60:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("Spatial rate limit unavailable — allowing request")


# PH6-060 — Rate limiting dependency for spatial endpoints (fail open).
# This code satisfies PH6-060. No additional functionality added.


# PKG-009 — API Dependencies & Endpoints: Implement verify_token dependency.
# This code satisfies PKG-009. No additional functionality added.
# PH6-045 — Rate limiting on /auth/token endpoint.
# This code satisfies PH6-045. No additional functionality added.
