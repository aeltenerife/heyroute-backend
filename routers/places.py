import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from models import SavedPlace

router = APIRouter()

class SavedPlaceCreate(BaseModel):
    user_id: str
    label: str
    location: str
    latitude: float
    longitude: float

@router.get("/{user_id}")
def get_places(user_id: str, db: Session = Depends(get_db)):
    """Fetch all saved places for a user."""
    try:
        parsed_user_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format. Must be UUID.")
        
    places = db.query(SavedPlace).filter(SavedPlace.user_id == parsed_user_id).all()
    return places

@router.post("/")
def create_place(payload: SavedPlaceCreate, db: Session = Depends(get_db)):
    """Save a new place."""
    try:
        parsed_user_id = uuid.UUID(payload.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format. Must be UUID.")

    db_place = SavedPlace(
        user_id=parsed_user_id,
        label=payload.label,
        location=payload.location,
        latitude=payload.latitude,
        longitude=payload.longitude
    )
    db.add(db_place)
    db.commit()
    db.refresh(db_place)
    return db_place

@router.delete("/{place_id}")
def delete_place(place_id: int, db: Session = Depends(get_db)):
    """Delete a saved place."""
    db_place = db.query(SavedPlace).filter(SavedPlace.id == place_id).first()
    if not db_place:
        raise HTTPException(status_code=404, detail="Saved place not found")
        
    db.delete(db_place)
    db.commit()
    return {"status": "ok", "deleted_id": place_id}

class SavedPlaceUpdate(BaseModel):
    label: str

@router.put("/{place_id}")
def update_place(place_id: int, payload: SavedPlaceUpdate, db: Session = Depends(get_db)):
    """Update a saved place label."""
    db_place = db.query(SavedPlace).filter(SavedPlace.id == place_id).first()
    if not db_place:
        raise HTTPException(status_code=404, detail="Saved place not found")
        
    db_place.label = payload.label
    db.commit()
    db.refresh(db_place)
    return db_place

