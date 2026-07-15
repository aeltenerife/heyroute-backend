import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from database import get_db
from models import TripHistory

router = APIRouter()

class TripHistoryCreate(BaseModel):
    user_id: str
    origin_coords: list
    destination_coords: list
    origin_name: str
    destination_name: str
    via_road_name: Optional[str] = None
    route_option: Optional[str] = None
    avoid_roads: Optional[list] = []
    avoid_features: Optional[list] = []

@router.get("/{user_id}")
def get_trip_history(user_id: str, db: Session = Depends(get_db)):
    """Fetch all trip history for a user."""
    try:
        parsed_user_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format. Must be UUID.")
        
    history = db.query(TripHistory).filter(TripHistory.user_id == parsed_user_id).order_by(desc(TripHistory.created_at)).all()
    return history

@router.post("/")
def create_trip_history(payload: TripHistoryCreate, db: Session = Depends(get_db)):
    """Insert a new trip record."""
    try:
        parsed_user_id = uuid.UUID(payload.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format. Must be UUID.")

    db_history = TripHistory(
        user_id=parsed_user_id,
        origin_coords=payload.origin_coords,
        destination_coords=payload.destination_coords,
        origin_name=payload.origin_name,
        destination_name=payload.destination_name,
        via_road_name=payload.via_road_name,
        route_option=payload.route_option,
        avoid_roads=payload.avoid_roads,
        avoid_features=payload.avoid_features
    )
    db.add(db_history)
    db.commit()
    db.refresh(db_history)
    return db_history

@router.delete("/{history_id}")
def delete_trip_history(history_id: int, db: Session = Depends(get_db)):
    """Delete a specific trip record."""
    db_history = db.query(TripHistory).filter(TripHistory.id == history_id).first()
    if not db_history:
        raise HTTPException(status_code=404, detail="Trip history not found")
        
    db.delete(db_history)
    db.commit()
    return {"status": "ok", "deleted_id": history_id}
