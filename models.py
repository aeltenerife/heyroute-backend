from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ARRAY
from sqlalchemy.dialects.postgresql import UUID
from database import Base
from datetime import datetime, timezone

class TripHistory(Base):
    __tablename__ = "trip_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), index=True)

    #  JSON arrays storing coordinates ([lat, lng])
    origin_coords = Column(JSON)
    destination_coords = Column(JSON)

    # Location names
    origin_name = Column(String)
    destination_name = Column(String, index=True)

    # Routing details
    via_road_name = Column(String)
    route_option = Column(String) # Ex. recommended, fastest, etc.

    # JSON arrays storing preferences
    avoid_roads = Column(ARRAY(String), default=list)
    avoid_features = Column(ARRAY(String), default=list)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

class SavedPlace(Base):
    __tablename__ = "places"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), index=True)
    label = Column(String)
    location = Column(String)
    latitude = Column(Float)
    longitude = Column(Float)