from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from models import TripHistory, SavedPlace
from collections import Counter
import os
import json

POINTS_CACHE_FILE = "road_points_cache.json"

async def check_user_preferences(db: AsyncSession, user_id: str, destination: str):
    # Gets this user's 10 most recent trips for this destination
    stmt = (
        select(TripHistory)
        .where(
            TripHistory.user_id == user_id, 
            TripHistory.destination_name == destination
        )
        .order_by(desc(TripHistory.created_at))
        .limit(10)
    )

    result = await db.execute(stmt)
    trips = result.scalars().all()

    if not trips:
        return None, None, None # No trips found for this user and destination
    
    # Count their historical choices
    all_avoided = []
    familiar_roads = []
    options = []

    for trip in trips:
        if trip.avoid_roads:
            all_avoided.extend(trip.avoid_roads)
        if trip.via_road_name:
            familiar_roads.append(trip.via_road_name)
        if trip.route_option:
            options.append(trip.route_option)
    
    # Calculate the most common occurence for each category
    most_avoided_road = Counter(all_avoided).most_common(1)[0][0] if all_avoided else None
    most_familiar_road = Counter(familiar_roads).most_common(1)[0][0] if familiar_roads else None
    most_preferred_option = Counter(options).most_common(1)[0][0] if options else None

    return most_avoided_road, most_familiar_road, most_preferred_option

async def resolve_semantic_location(db: AsyncSession, user_id: str, destination_label: str):
    stmt = (
        select(SavedPlace)
        .where(
            SavedPlace.user_id == user_id,
            SavedPlace.label == destination_label.lower()
        )
    )
    result = await db.execute(stmt)
    saved_place = result.scalar_one_or_none()

    if saved_place:
        return {"lat": saved_place.latitude, "lng": saved_place.longitude}
    return None

async def load_polygons(road_name: str):
    return None

async def store_polygons(road_name: str, multi_poly):
    pass

async def load_road_points(road_name: str) -> list:
    """
    Retrieves stored road centerline points for Mapbox avoidance.
    """
    if os.path.exists(POINTS_CACHE_FILE):
        try:
            with open(POINTS_CACHE_FILE, "r") as f:
                cache = json.load(f)
                return cache.get(road_name.lower())
        except Exception:
            pass
    return None

async def store_road_points(road_name: str, points: list):
    """
    Stores road centerline points for Mapbox avoidance.
    """
    cache = {}
    if os.path.exists(POINTS_CACHE_FILE):
        try:
            with open(POINTS_CACHE_FILE, "r") as f:
                cache = json.load(f)
        except Exception:
            pass
    cache[road_name.lower()] = points
    try:
        with open(POINTS_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"Failed to write road points cache: {e}")