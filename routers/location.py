"""
This module handles location update functionality for HeyRoute.

Handles:
    - Receiving current location updates from the client.
    - Storing the latest location per session for use by other endpoints.
"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# Shared location store keyed by session ID.
# Other modules can import this to read the latest location for a session.
LOCATION_STORE = {}

class LocationUpdate(BaseModel):
    lat: float
    lng: float
    session_id: str

@router.post("/update")
def update_current_location(payload: LocationUpdate):
    """
    Endpoint to receive current location updates from the client
    and store them for use by routing and navigation endpoints.
    """

    LOCATION_STORE[payload.session_id] = {"lat": payload.lat, "lng": payload.lng}
    return {"status": "ok", "session_id": payload.session_id}
