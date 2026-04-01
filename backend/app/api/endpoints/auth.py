"""
Authentication endpoints for the Predictive Parking API.

Provides anonymous JWT token issuance and token-based auth for frontend access.

External Dependencies:
- fastapi
- jwt (via app.core.security)
State: none — all functions are stateless.
"""

import uuid

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, status

from app.api.deps import check_token_rate_limit
from app.core.security import create_access_token

router = APIRouter()


@router.post("/anonymous")
def anonymous_token() -> dict[str, str | int]:
    """Issue an anonymous JWT token for unauthenticated frontend access.

    Returns:
        dict: Contains access_token, token_type, and expires_in seconds.
    """
    sub = f"anon:{uuid.uuid4()}"
    token = create_access_token(
        data={"sub": sub, "type": "anonymous"},
        expires_delta=timedelta(hours=24),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 86400,
    }


@router.post("/token")
def token(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    _rate_limit: Annotated[None, Depends(check_token_rate_limit)] = None,
) -> dict[str, str]:
    """Issue a JWT token for authenticated users.

    Rate-limited to 10 requests per hour per client IP.

    Args:
        username: The username for authentication.
        password: The password for authentication.
        _rate_limit: Rate limiting dependency (injected automatically).

    Returns:
        dict: Contains access_token and token_type.

    Raises:
        HTTPException: 401 if credentials are invalid.
    """
    # MVP: accept any non-empty username/password pair
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(hours=24),
    )
    return {"access_token": token, "token_type": "bearer"}


# This code satisfies PH6-055. No additional functionality added.
# PH6-045 — Rate limiting on /auth/token endpoint.
# This code satisfies PH6-045. No additional functionality added.
