"""
This module implements the Google Maps Routes API adapter for HeyRoute.

Handles:
    - Geocoding: Converting place names to coordinates using Geoapify API.
    - Reverse Geocoding: Converting coordinates to human-readable addresses using Geoapify API.
    - Directions: Fetching traffic-aware routes from origin to destination
      using the Google Maps Routes API v2.

Note on road-specific avoidance:
    Unlike the OpenRouteService adapter, Google Maps Routes API does NOT support
    polygon-based road avoidance (e.g., blocking a specific road like EDSA).
    It only supports category-level avoidance (tolls, highways, ferries).
    Traffic-aware routing inherently accounts for congestion, reducing the need
    for manual road avoidance in most cases.
"""

import os
import time
import httpx
import polyline
from typing import List
from adapters.adapter import APIAdapter
from adapters.openrouteservice_adapter import extract_via_road, sample_smartly


# --- Routing Preference Mapping ---
# Maps HeyRoute's route options to Google Maps routing preferences
PREFERENCE_MAP = {
    "recommended": "TRAFFIC_AWARE",          # Balanced traffic-aware routing
    "fastest":     "TRAFFIC_AWARE_OPTIMAL",  # Strongly optimizes for real-time traffic
    "shortest":    "TRAFFIC_UNAWARE",        # Ignores traffic, tends toward direct routes
}

# --- Field Mask ---
# Controls which fields Google Maps returns (and which you are billed for).
# Only request what is needed to keep costs low.
FIELD_MASK = ",".join([
    "routes.duration",
    "routes.staticDuration",
    "routes.distanceMeters",
    "routes.polyline.encodedPolyline",
    "routes.legs.steps.navigationInstruction.instructions",
    "routes.legs.steps.startLocation",
    "routes.description",
])


def find_nearest_index(full_coords: list, lat: float, lng: float) -> int:
    """
    Finds the index of the point in full_coords closest to the given lat/lng.

    Purpose:
    - Google Maps returns step start locations as lat/lng coordinates.
    - ORS returned explicit waypoint indices. This function converts Google's
      format to the same index-based format expected by the rest of the app.

    Parameters:
        full_coords (list): List of [lng, lat] pairs (full decoded route polyline).
        lat (float): Target latitude.
        lng (float): Target longitude.

    Returns:
        int: Index of the nearest point in full_coords.
    """
    min_dist = float('inf')
    min_idx = 0
    for i, coord in enumerate(full_coords):
        # coord is [lng, lat]
        dlat = coord[1] - lat
        dlng = coord[0] - lng
        dist = dlat * dlat + dlng * dlng
        if dist < min_dist:
            min_dist = dist
            min_idx = i
    return min_idx


class GoogleRoutesAdapter(APIAdapter):
    """
    Adapter for Google Maps Routes API providing traffic-aware Geocoding and Directions.

    Implements the same APIAdapter interface as OpenRouteServiceAdapter,
    making it a drop-in replacement with no changes required in model.py.
    """

    def __init__(self):
        self.client = httpx.AsyncClient()
        self.google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        self.geoapify_api_key = os.getenv("GEOAPIFY_API_KEY")

        # Geoapify endpoints (unchanged from ORS adapter)
        self.geocode_url = "https://api.geoapify.com/v1/geocode/search"
        self.reverse_geocode_url = "https://api.geoapify.com/v1/geocode/reverse"

        # Google Maps Routes API endpoint
        self.directions_url = "https://routes.googleapis.com/directions/v2:computeRoutes"

    # ---------- Geocoding ----------
    async def geocode(self, place_name: str) -> dict:
        """
        Geocodes a place name to latitude and longitude using Geoapify.

        Returns:
            dict: {lat, lng} if successful, None otherwise.
        """
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
        """
        Reverse geocodes coordinates to a human-readable address using Geoapify.

        Returns:
            str: Formatted address string, or "lat,lng" as a fallback.
        """
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
        """
        Fetches traffic-aware driving routes using the Google Maps Routes API v2.

        Key differences from ORS adapter:
        - Duration is real-time traffic-aware (not static speed-limit based).
        - avoid_roads (specific road polygon blocking) is NOT supported by Google.
          If specified, a warning is logged and traffic routing handles it naturally.
        - avoid_features (tollways, highways, ferries) IS supported via routeModifiers.
        - An extra 'static_duration' field is returned for reference (no-traffic ETA).

        Returns:
            - List[dict]: routes in the same format as the ORS adapter output
            - float: time taken for the Google API call in milliseconds
        """

        # Build origin and destination in Google's LatLng format
        payload = {
            "origin": {
                "location": {
                    "latLng": {
                        "latitude": origin["lat"],
                        "longitude": origin["lng"]
                    }
                }
            },
            "destination": {
                "location": {
                    "latLng": {
                        "latitude": destination["lat"],
                        "longitude": destination["lng"]
                    }
                }
            },
            "travelMode": "DRIVE",
            "routingPreference": PREFERENCE_MAP.get(
                (option or "recommended").strip().lower(),
                "TRAFFIC_AWARE"
            ),
            # Only request alternatives when there are no via waypoints
            # (Google Maps doesn't return alternatives when intermediates are present)
            "computeAlternativeRoutes": not bool(via),
            "languageCode": "en-US",
            "units": "METRIC"
        }

        # Add via (intermediate) waypoints if provided
        if via:
            payload["intermediates"] = [
                {
                    "location": {
                        "latLng": {
                            "latitude": v["lat"],
                            "longitude": v["lng"]
                        }
                    }
                }
                for v in via
            ]

        # Map avoid_features to Google's routeModifiers
        avoid_tolls    = "tollways" in (avoid_features or [])
        avoid_highways = "highways" in (avoid_features or [])
        avoid_ferries  = "ferries"  in (avoid_features or [])

        payload["routeModifiers"] = {
            "avoidTolls":    avoid_tolls,
            "avoidHighways": avoid_highways,
            "avoidFerries":  avoid_ferries,
        }

        # Log unsupported road-specific avoidance (Google Maps doesn't support polygons)
        if avoid_roads:
            print(
                f"[GoogleRoutesAdapter] Note: Specific road avoidance {avoid_roads} "
                f"is not supported by the Google Maps Routes API. "
                f"Traffic-aware routing will naturally route around congested roads."
            )

        headers = {
            "Content-Type":   "application/json",
            "X-Goog-Api-Key": self.google_api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }

        start_time = time.perf_counter()
        resp = await self.client.post(self.directions_url, json=payload, headers=headers)
        end_time = time.perf_counter()
        google_ms = (end_time - start_time) * 1000

        if resp.status_code != 200:
            error_detail = resp.text
            try:
                error_data = resp.json()
                error_detail = error_data.get("error", {}).get("message", error_detail)
            except Exception:
                pass
            raise RuntimeError(
                f"Google Routes API failed ({resp.status_code}): {error_detail}"
            )

        data = resp.json()
        
        if not data.get("routes"):
            print(f"[GoogleRoutesAdapter] Warning: No routes returned for {origin} -> {destination}")
            print(f"[GoogleRoutesAdapter] Payload: {payload}")
            print(f"[GoogleRoutesAdapter] Response: {data}")

        routes_output = []

        for route in data.get("routes", []):
            encoded_geom = route.get("polyline", {}).get("encodedPolyline", "")
            full_coords = []
            matching_coords = []
            mapbox_waypoints = []
            turn_indices = []

            if encoded_geom:
                # Decode the polyline: returns (lat, lng) pairs; swap to [lng, lat]
                # for consistency with Mapbox and ORS formats
                decoded = polyline.decode(encoded_geom)
                full_coords = [[lng, lat] for lat, lng in decoded]

                # Compute turn_indices from step startLocations
                # (Google Maps doesn't provide explicit waypoint indices like ORS)
                steps = route.get("legs", [{}])[0].get("steps", [])
                for step in steps:
                    start_loc = step.get("startLocation", {}).get("latLng", {})
                    if start_loc:
                        idx = find_nearest_index(
                            full_coords,
                            start_loc["latitude"],
                            start_loc["longitude"]
                        )
                        turn_indices.append(idx)

                # Smart-sample to 100 points while preserving all turn points
                matching_coords, mapbox_waypoints = await sample_smartly(
                    full_coords, turn_indices, limit=100
                )

            # Extract step-by-step navigation instructions
            steps = route.get("legs", [{}])[0].get("steps", [])
            steps_instructions = [
                step.get("navigationInstruction", {}).get("instructions", "")
                for step in steps
            ]

            # Extract the main via road from instructions
            # (reuses same regex logic as ORS adapter: finds "onto [road name]")
            via_road = await extract_via_road(steps_instructions)

            # Parse traffic-aware duration: Google returns "1800s" → minutes
            duration_str = route.get("duration", "0s")
            duration_secs = int(duration_str.rstrip("s"))
            duration_mins = int(duration_secs / 60)

            # Parse static (no-traffic) duration for comparison
            static_str = route.get("staticDuration", "0s")
            static_secs = int(static_str.rstrip("s"))
            static_mins = int(static_secs / 60)

            # Parse distance: meters → km
            distance_m = route.get("distanceMeters", 0)
            distance_km = int(distance_m / 1000)

            routes_output.append({
                "via":             via_road,
                "start":           origin,
                "end":             destination,
                "distance":        f"{distance_km} km",
                "duration":        f"{duration_mins} mins",    # traffic-aware ETA
                "static_duration": f"{static_mins} mins",      # no-traffic ETA (bonus)
                "full_geometry":   full_coords,
                "matching_coords": matching_coords,
                "waypoint_indices": mapbox_waypoints,
                "turn_indices":    turn_indices,
                "steps_instructions": steps_instructions,
            })

        return routes_output, google_ms
