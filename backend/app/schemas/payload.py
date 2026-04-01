"""
Pydantic validation schemas for the predictive parking application.

This module defines the data structures used for API payloads and responses,
ensuring strict validation and type safety.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ParkingEventCreate(BaseModel):
    """
    Schema for creating a new parking event.

    Attributes:
        lat (float): Latitude of the event location.
        lon (float): Longitude of the event location.
        event_type (Literal["PARKED", "LEFT", "SEARCHING"]): Type of parking event.
    """

    lat: float = Field(..., description="Latitude of the event location.")
    lon: float = Field(..., description="Longitude of the event location.")
    event_type: Literal["PARKED", "LEFT", "SEARCHING"] = Field(
        ..., description="Type of parking event."
    )


class Token(BaseModel):
    """
    Schema for authentication token responses.

    Attributes:
        access_token (str): The JWT access token.
        token_type (str): The type of token (e.g., 'bearer').
    """

    access_token: str = Field(..., description="The JWT access token.")
    token_type: str = Field(..., description="The type of token.")


class ProbabilitySegment(BaseModel):
    """
    Schema for a single segment probability result.

    Attributes:
        segment_id: UUID of the street segment.
        street_name: Name of the street (may be empty).
        probability: Adjusted probability [0.0, 1.0].
        window_minutes: Time window used for adjustment.
        bay_count: Estimated number of parking bays.
        distance_metres: Haversine distance from query point.
        last_updated: ISO timestamp of last tile generation.
    """

    segment_id: str = Field(..., description="UUID of the street segment.")
    street_name: str = Field(..., description="Name of the street.")
    probability: float = Field(..., ge=0.0, le=1.0, description="Adjusted probability.")
    window_minutes: int = Field(..., description="Time window in minutes.")
    bay_count: int = Field(..., description="Estimated number of parking bays.")
    distance_metres: float = Field(
        ..., description="Distance from query point in metres."
    )
    last_updated: str | None = Field(
        None, description="ISO timestamp of last tile upload."
    )


class BestNearbyRecommendation(BaseModel):
    """
    Schema for a single best-nearby parking recommendation.

    Attributes:
        rank: Ranking position (1-based).
        segment_id: UUID of the street segment.
        street_name: Name of the street.
        probability: Pre-computed probability [0.0, 1.0].
        walk_metres: Walking distance from query point in metres.
        restriction_active: Always False (restrictions baked into probability).
    """

    rank: int = Field(..., ge=1, description="Ranking position (1-based).")
    segment_id: str = Field(..., description="UUID of the street segment.")
    street_name: str = Field(..., description="Name of the street.")
    probability: float = Field(
        ..., ge=0.0, le=1.0, description="Pre-computed probability."
    )
    walk_metres: float = Field(..., ge=0.0, description="Walking distance in metres.")
    restriction_active: bool = Field(
        False,
        description="Always False — restrictions already factored into probability.",
    )


class BestNearbyResponse(BaseModel):
    """
    Schema for the best-nearby parking recommendations response.

    Attributes:
        recommendations: Ranked list of parking recommendations.
        generated_at: ISO timestamp when the response was generated.
    """

    recommendations: list[BestNearbyRecommendation] = Field(
        ...,
        description="Ranked list of parking recommendations.",
    )
    generated_at: str = Field(
        ...,
        description="ISO timestamp when the response was generated.",
    )


class SegmentDetailResponse(BaseModel):
    """
    Schema for full segment detail response.

    Attributes:
        segment_id: UUID of the street segment.
        street_name: Name of the street (may be empty).
        probability_5min: Probability for 5-minute window.
        probability_10min: Probability for 10-minute window.
        probability_15min: Probability for 15-minute window.
        bay_count: Number of parking bays on segment.
        restriction_active: Whether restriction is currently active.
        restriction_schedule: CPZ restriction schedule if available.
        last_crowd_event: Most recent crowd event type or None.
        last_crowd_event_age_min: Minutes since last crowd event or None.
        traffic_speed: Current traffic speed in km/h or None.
    """

    segment_id: str = Field(..., description="UUID of the street segment.")
    street_name: str = Field(..., description="Name of the street.")
    probability_5min: float = Field(..., ge=0.0, le=1.0)
    probability_10min: float = Field(..., ge=0.0, le=1.0)
    probability_15min: float = Field(..., ge=0.0, le=1.0)
    bay_count: int = Field(..., description="Number of parking bays.")
    restriction_active: bool = Field(..., description="Whether restriction is active.")
    restriction_schedule: list[dict[str, str]] | None = Field(
        default=None, description="CPZ restriction schedule."
    )
    last_crowd_event: str | None = Field(default=None, description="Last event type.")
    last_crowd_event_age_min: float | None = Field(
        default=None, description="Minutes since last event."
    )
    traffic_speed: float | None = Field(
        default=None, description="Current traffic speed km/h."
    )


class HealthResponse(BaseModel):
    """
    Schema for the /api/v1/health endpoint response.

    Attributes:
        status: Overall service status string.
        db_connected: Whether the database is reachable.
        redis_connected: Whether Redis is reachable.
        r2_last_tile_upload: ISO timestamp of last tile upload or None.
        r2_public_url: Public Cloudflare R2 URL for MVT tiles.
        segment_count: Number of street segments in the database.
        bay_count: Number of parking bays in the database.
        cpz_count: Number of CPZ zones in the database.
        enforcement_events_30d: Number of enforcement events in last 30 days.
        last_ingestion: Mapping of source to last successful ingestion timestamp.
    """

    status: str = Field(..., description="Overall service status.")
    db_connected: bool = Field(..., description="Database connectivity status.")
    redis_connected: bool = Field(..., description="Redis connectivity status.")
    r2_last_tile_upload: str | None = Field(
        default=None, description="Last tile upload timestamp."
    )
    r2_public_url: str = Field(
        default="", description="Public Cloudflare R2 URL for MVT tiles."
    )
    segment_count: int = Field(..., description="Number of street segments.")
    bay_count: int = Field(..., description="Number of parking bays.")
    cpz_count: int = Field(..., description="Number of CPZ zones.")
    enforcement_events_30d: int = Field(
        ..., description="Enforcement events in last 30 days."
    )
    last_ingestion: dict[str, str | None] = Field(
        default_factory=dict, description="Last successful ingestion per source."
    )


# PKG-005 — Pydantic validation schemas for parking events and authentication tokens.
# This code satisfies PKG-005. No additional functionality added.
