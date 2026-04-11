"""
Shared HTTP session factory and ingestion utilities.

External dependencies:
    requests, urllib3, app.core.config.

State: None — all functions are stateless.
"""

import logging

import requests  # type: ignore[import-untyped]
from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Street name abbreviation mapping for normalisation
STREET_ABBREVS: dict[str, str] = {
    "RD": "ROAD",
    "ST": "STREET",
    "AVE": "AVENUE",
    "LN": "LANE",
    "DR": "DRIVE",
    "PL": "PLACE",
    "SQ": "SQUARE",
    "CT": "COURT",
    "CL": "CLOSE",
}


def normalise_street(name: str) -> str:
    """Normalise a street name for fuzzy matching.

    Converts to uppercase, expands common abbreviations.

    Args:
        name: Raw street name from PCN data or OSM.
    Returns:
        Normalised uppercase string with abbreviations expanded.
    """
    parts = name.upper().split()
    return " ".join(STREET_ABBREVS.get(p, p) for p in parts)


def get_http_session() -> requests.Session:
    """Create a requests.Session with retry logic and sensible defaults.

    Returns:
        Configured requests.Session ready for API calls.
    """
    session = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": "parkd-in/1.0",
        }
    )

    return session
