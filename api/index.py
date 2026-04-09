"""
Vercel serverless entry point — minimal LOCAL_MODE FastAPI app.

This is a stripped-down version of the full app that only needs
fastapi + pydantic-settings. No DB, Redis, R2, or scheduler.

Tiles are served as static files from public/tiles/ by Vercel's CDN
(no code path here for tiles at all).

The API endpoints exposed here are:
  GET  /api/v1/health         — returns static healthy status
  POST /api/v1/auth/anonymous — returns a placeholder JWT (map loads fine)
  POST /api/v1/parking/event  — accepts reports, logs them (no DB write)
  GET  /api/v1/parking/segment/{id} — returns placeholder segment data
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Parkd-In API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
async def health():
    return {
        "status": "healthy",
        "mode": "local",
        "db_connected": False,
        "redis_connected": False,
        "segment_count": 8512,
        "bay_count": 8205,
        "cpz_count": 20,
        "enforcement_events_30d": 0,
    }


@app.post("/api/v1/auth/anonymous")
async def anonymous_token():
    # Return a static placeholder token — map JS only needs something truthy
    import time, base64, json
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "anon", "exp": int(time.time()) + 86400}).encode()
    ).decode().rstrip("=")
    return {"access_token": f"eyJ.{payload}.local", "token_type": "bearer"}


class ParkingEvent(BaseModel):
    lat: float
    lon: float
    event_type: str


@app.post("/api/v1/parking/event")
async def parking_event(event: ParkingEvent):
    # Accept the report — nothing to write to in LOCAL_MODE
    return {"status": "accepted", "mode": "local"}


@app.get("/api/v1/parking/segment/{segment_id}")
async def segment_detail(segment_id: str):
    # Return placeholder data so popups render something meaningful
    return {
        "segment_id": segment_id,
        "street_name": "Camden Street",
        "probability_5min": 0.72,
        "probability_10min": 0.68,
        "probability_15min": 0.61,
        "bay_count": 4,
        "restriction_active": False,
        "last_crowd_event": None,
        "last_crowd_event_age_min": None,
    }
