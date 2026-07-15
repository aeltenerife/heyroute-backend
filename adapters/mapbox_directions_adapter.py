"""
This module implements the Mapbox Directions adapter for HeyRoute.

Handles:
    - Geocoding: Converting place names to coordinates using Geoapify API.
    - Reverse Geocoding: Converting coordinates to human-readable addresses using Geoapify API.
    - Directions: Fetching traffic-aware routes from origin to destination
      using the Mapbox Directions API.

Note on road-specific avoidance:
    Mapbox Directions API supports `exclude=point(lng lat)`.
    We will load road geometries from the database and sample points to block routing along those roads.
"""

import os
import time
import httpx
import polyline
import numpy as np
from typing import List
from adapters.adapter import APIAdapter
from adapters.openrouteservice_adapter import extract_via_road, sample_smartly, get_road_polygon
from db import load_polygons, store_polygons
from shapely.geometry import Point

# --- Routing Preference Mapping ---
PREFERENCE_MAP = {
    "recommended": "driving-traffic",  # Best for general use
    "fastest":     "driving-traffic",  # Optimal with traffic
    "shortest":    "driving",          # Ignores traffic (shorter distance)
}

class MapboxDirectionsAdapter(APIAdapter):
    def __init__(self):
        self.client = httpx.AsyncClient()
        self.mapbox_api_key = os.getenv("MAPBOX_ACCESS_TOKEN")
        self.geoapify_api_key = os.getenv("GEOAPIFY_API_KEY")

        # Geoapify endpoints
        self.geocode_url = "https://api.geoapify.com/v1/geocode/search"
        self.reverse_geocode_url = "https://api.geoapify.com/v1/geocode/reverse"

        # Mapbox Directions API endpoint base
        self.directions_url_base = "https://api.mapbox.com/directions/v5/mapbox"

    # ---------- Geocoding ----------
    async def geocode(self, place_name: str) -> dict:
        if not place_name:
            return None
        params = {
            "text": place_name, 
            "apiKey": self.geoapify_api_key,
            "filter": "countrycode:ph"
        }
        try:
            resp = await self.client.get(self.geocode_url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("features"):
                    coords = data["features"][0]["geometry"]["coordinates"]
                    return {"lat": coords[1], "lng": coords[0]}
        except Exception as e:
            print(f"Geocoding error for {place_name}: {e}")
        return None

    # ---------- Reverse Geocoding ----------
    async def reverse_geocode(self, lat: float, lng: float) -> str:
        if not lat or not lng:
            return None
        params = {"lat": lat, "lon": lng, "apiKey": self.geoapify_api_key}
        try:
            resp = await self.client.get(self.reverse_geocode_url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("features"):
                    return data["features"][0]["properties"].get("formatted", f"{lat},{lng}")
            return f"{lat},{lng}"
        except Exception as e:
            print(f"Reverse Geocoding error for {lat}, {lng}: {e}")

    # ---------- Helper for Road Avoidance ----------
    async def _get_exclusion_points(self, avoid_roads: List[str]) -> List[str]:
        """
        Takes a list of road names to avoid.
        Loads their polygons (fetching via OSM if not cached), and samples points
        along them to create Mapbox `point(lng lat)` exclusion strings.
        Mapbox allows up to 50 point exclusions. We'll sample up to ~10-15 per road.
        """
        if not avoid_roads:
            return []
            
        points_to_exclude = []
        points_per_road = min(20, 50 // len(avoid_roads)) if len(avoid_roads) > 0 else 20
        
        for road in avoid_roads:
            poly_data = load_polygons(road)
            if not poly_data:
                print(f"[Mapbox Adapter] Polygons for {road} not found in DB. Fetching from OSM...")
                geom = await get_road_polygon(self.client, road)
                if geom:
                    store_polygons(road, geom)
                    poly_data = geom
                else:
                    print(f"[Mapbox Adapter] Could not fetch geometry for {road}. Skipping exclusion.")
                    continue
            
            # Extract points from the polygon boundaries or centroids
            if poly_data:
                try:
                    if hasattr(poly_data, 'geoms'):
                        geoms = list(poly_data.geoms)
                    else:
                        geoms = [poly_data]
                    
                    # Sort by area or length to prioritize main segments
                    geoms.sort(key=lambda g: g.area, reverse=True)
                    
                    sampled_count = 0
                    for geom in geoms:
                        if sampled_count >= points_per_road:
                            break
                        
                        # Get exterior coordinates
                        coords = list(geom.exterior.coords)
                        if len(coords) > 0:
                            step = max(1, len(coords) // (points_per_road - sampled_count))
                            for i in range(0, len(coords), step):
                                if sampled_count >= points_per_road:
                                    break
                                lng, lat = coords[i]
                                points_to_exclude.append(f"point({lng} {lat})")
                                sampled_count += 1
                except Exception as e:
                    print(f"[Mapbox Adapter] Error extracting points from polygon for {road}: {e}")
                    
        return points_to_exclude

    # ---------- Directions ----------
    async def get_directions(
        self,
        origin: dict,
        destination: dict,
        option: str,
        via: List[dict] = None,
        avoid_roads: List[str] = None,
        avoid_features: List[str] = None,
    ) -> List[dict]:
        
        profile = PREFERENCE_MAP.get((option or "recommended").strip().lower(), "driving-traffic")
        
        # Format coordinates string: lng,lat;lng,lat
        coords_list = [f"{origin['lng']},{origin['lat']}"]
        if via:
            for v in via:
                coords_list.append(f"{v['lng']},{v['lat']}")
        coords_list.append(f"{destination['lng']},{destination['lat']}")
        
        coordinates_str = ";".join(coords_list)
        
        url = f"{self.directions_url_base}/{profile}/{coordinates_str}"
        
        # Build query parameters
        params = {
            "access_token": self.mapbox_api_key,
            "alternatives": "true" if not via else "false",
            "geometries": "polyline",
            "overview": "full",
            "steps": "true",
            "language": "en"
        }
        
        # Handle Exclusions
        excludes = []
        if avoid_features:
            if "tollways" in avoid_features:
                excludes.append("toll")
            if "highways" in avoid_features:
                excludes.append("motorway")
            if "ferries" in avoid_features:
                excludes.append("ferry")
                
        if avoid_roads:
            exclusion_points = await self._get_exclusion_points(avoid_roads)
            excludes.extend(exclusion_points)
            
        if excludes:
            # Mapbox accepts comma-separated list of exclusions
            params["exclude"] = ",".join(excludes)

        start_time = time.perf_counter()
        resp = await self.client.get(url, params=params)
        end_time = time.perf_counter()
        mapbox_ms = (end_time - start_time) * 1000

        if resp.status_code != 200:
            error_detail = resp.text
            try:
                error_data = resp.json()
                error_detail = error_data.get("message", error_detail)
            except Exception:
                pass
            raise RuntimeError(f"Mapbox Directions API failed ({resp.status_code}): {error_detail}")

        data = resp.json()
        
        if not data.get("routes"):
            print(f"[Mapbox Adapter] Warning: No routes returned for {origin} -> {destination}")
            
        routes_output = []

        for route in data.get("routes", []):
            encoded_geom = route.get("geometry", "")
            full_coords = []
            matching_coords = []
            mapbox_waypoints = []
            turn_indices = []

            if encoded_geom:
                # Mapbox default polyline precision is 5. Returns (lat, lng). Swap to [lng, lat]
                decoded = polyline.decode(encoded_geom)
                full_coords = [[lng, lat] for lat, lng in decoded]

            steps_instructions = []
            distance_m = route.get("distance", 0)
            duration_secs = route.get("duration", 0) # Real-time if driving-traffic
            static_duration_secs = route.get("duration_typical", duration_secs) # Historic/typical

            for leg in route.get("legs", []):
                for step in leg.get("steps", []):
                    # Instruction
                    instruction = step.get("maneuver", {}).get("instruction", "")
                    if instruction:
                        steps_instructions.append(instruction)
                    
                    # Turn indices logic
                    start_loc = step.get("maneuver", {}).get("location")
                    if start_loc and full_coords:
                        min_dist = float('inf')
                        min_idx = 0
                        slng, slat = start_loc[0], start_loc[1]
                        for i, coord in enumerate(full_coords):
                            dlat = coord[1] - slat
                            dlng = coord[0] - slng
                            dist = dlat * dlat + dlng * dlng
                            if dist < min_dist:
                                min_dist = dist
                                min_idx = i
                        turn_indices.append(min_idx)

            if full_coords:
                matching_coords, mapbox_waypoints = await sample_smartly(
                    full_coords, turn_indices, limit=100
                )

            # Extract via road
            via_road = await extract_via_road(steps_instructions)
            
            # Format outputs
            duration_mins = int(duration_secs / 60)
            static_mins = int(static_duration_secs / 60)
            distance_km = round(distance_m / 1000, 1)

            routes_output.append({
                "via":             via_road if via_road else "Main Route",
                "start":           origin,
                "end":             destination,
                "distance":        f"{distance_km} km",
                "duration":        f"{duration_mins} mins",
                "static_duration": f"{static_mins} mins",
                "full_geometry":   full_coords,
                "matching_coords": matching_coords,
                "waypoint_indices": mapbox_waypoints,
                "turn_indices":    turn_indices,
                "steps_instructions": steps_instructions,
                "exclude_string": ",".join(excludes) if excludes else None,
            })

        return routes_output, mapbox_ms
