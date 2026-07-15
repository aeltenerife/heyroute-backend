"""
This module implements the OpenRouteService adapter for HeyRoute.

Handles:
    - Geocoding: Converting place names to coordinates using Geoapify API.
    - Reverse Geocoding: Converting coordinates to human-readable addresses using Geoapify API.
    - Directions: Fetching routes from origin to destination with options for avoiding specific roads or features.
    - Building "avoid_polygons" for specified roads by fetching road geometries from OpenStreetMap via the Overpass API.
"""

import asyncio
import os
import httpx
import polyline
import re
import time
import numpy as np
from rdp import rdp
from typing import List
from shapely.geometry import LineString, MultiPolygon
from collections import Counter
from adapters.adapter import APIAdapter
from db import load_polygons, store_polygons

# OpenStreetMap Overpass API endpoint for fetching road geometries and nearby features
OVERPASS_URL = "http://overpass-api.de/api/interpreter"

async def get_road_polygon(client, road_name: str):
    """
    Fetches OpenStreetMap 'ways' for a specific road and generates a buffered MultiPolygon. 

    Purpose:
    - To generate "avoid" areas for a specific road by converting road segments into polygons.
    - These polygons are then used to create "avoid_polygons" for the OpenRouteService API, which helps in generating alternative routes that steer clear of the specified roads.

    Key Processing Steps:
    1. Fetch the geometry of the target road using an Overpass API query.
    2. Fetch nearby roads to identify intersections and layer differences.
    3. Filter out:
        - Segments that intersect with roads of different layers (e.g., underpasses/overpasses) to avoid creating polygons that block the wrong road levels.
        - Segments that start or end at traffic signals (likely intersections rather than continuous road segments) to ensure routing remains flexible at intersections.
    4. Buffer the remaining segments to create polygons that represent the "avoid" areas for the specified road.
    5. Store the generated polygons to reduce the need for repeated Overpass queries and improving performance on the following requests for the same road.

    Returns:
        A Shapely MultiPolygon object representing the buffered avoid areas for the specified road.
        None: if no valid polygons could be created.
    """

    # Hardcoded bounding limits for Metro Manila to limit the Overpass query area and improve performance.
    bbox = "14.402,120.917,14.810,121.150"

    # Fetch the target road geometry
    road_query = f"""
    [out:json][timeout:25];
    way["highway"]["name"="{road_name}"]({bbox});
    (._;>;);
    out tags geom;
    """

    # Fetch nearby roads for intersections and layer filtering
    nearby_query = f"""
    [out:json][timeout:25];
    way["highway"]["name"="{road_name}"]({bbox})->.targetRoad;
    way["highway"](around.targetRoad:30)({bbox})["name"];
    (._;>;);
    out tags geom;
    """

    try:
        # Execute API requests
        road_resp, nearby_resp = await asyncio.gather(
            client.get(OVERPASS_URL, params={"data": road_query}, timeout=30.0),
            client.get(OVERPASS_URL, params={"data": nearby_query}, timeout=30.0)
        )
        road_resp.raise_for_status()
        nearby_resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            print("Error: You are being rate-limited. Slow down your requests.")
        elif e.response.status_code == 504:
            print("Error: The query took too long (Gateway Timeout). Simplify your query.")
        else:
            print(f"HTTP Error {e.response.status_code}: {e.response.text}")
    except httpx.ConnectError:
        print("Error: Could not connect to the Overpass server. Check your internet or URL.")
    except Exception as e:
        print(f"An unexpected error occurred: {type(e).__name__} - {str(e)}")
        return None

    road_data = road_resp.json()
    nearby_data = nearby_resp.json()

    # Convert road segments into Shapely LineStrings by layer
    road_lines = []
    layer_counts = {}
    for elem in road_data.get("elements", []):
        if elem["type"] == "way" and "geometry" in elem:
            layer = int(elem["tags"].get("layer", "0"))
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
            coords = [(pt["lon"], pt["lat"]) for pt in elem["geometry"]]
            if len(coords) > 1:
                road_lines.append((layer, LineString(coords)))

    if not road_lines:
        print(f"No road geometry found for {road_name}")
        return None

    # Convert nearby roads into Shapely LineStrings
    nearby_lines = []
    for elem in nearby_data.get("elements", []):
        if elem["type"] == "way" and "geometry" in elem:
            coords = [(pt["lon"], pt["lat"]) for pt in elem["geometry"]]
            if len(coords) > 1:
                layer = int(elem["tags"].get("layer", "0"))
                nearby_lines.append((layer, LineString(coords)))

    # Create polygons and skips any segment that intersects another road of *different* layer
    polygons = []

    # Precompute a set of traffic signal nodes from nearby roads
    traffic_nodes = set()
    for elem in nearby_data.get("elements", []):
        if elem["type"] == "node":
            if elem.get("tags", {}).get("highway") == "traffic_signals":
                traffic_nodes.add((elem["lon"], elem["lat"]))

    # Process each road segment
    for layer, line in road_lines:
        skip_segment = False
        # Check for nearby road intersections with different layers
        for near_layer, near_line in nearby_lines:
            if line.intersects(near_line):
                if near_layer != layer:
                    skip_segment = True
                    break

        if skip_segment:
            continue

        # Check if both start and end points have traffic signals
        start_point = (line.coords[0][0], line.coords[0][1])
        end_point = (line.coords[-1][0], line.coords[-1][1])
        if start_point in traffic_nodes or end_point in traffic_nodes:
            continue

        # Buffer the kept segment into a polygon
        buffered = line.buffer(0.00004)

        # Simplify the polygon to reduce complexity while preserving shape
        simplified = buffered.simplify(0.000005, preserve_topology=True)
        
        polygons.append(simplified)

    if not polygons:
        print(f"No valid polygons for {road_name} (all segments intersect higher/lower layers)")
        return None

    multi_poly = MultiPolygon(polygons)

    # Store computed polygons in the database for future reuse
    await store_polygons(road_name, multi_poly)
    
    print(f"Created {len(polygons)} polygons for {road_name}")
    return multi_poly

async def extract_via_road(steps: list) -> str:
    """
    Determines the main 'via' road from step instructions.

    Purpose:
    - Provides a concise "via [road name]" summary for each route by analyzing the step instructions returned by OpenRouteService.
    - Helps users quickly identify the key roads involved in the route. 

    Key Processing Steps:
    1. Use a regular expression to extract road names from step instructions that contain "onto [road name]".
    2. Count the frequency of each extracted road name across all steps.
    3. Identify the most frequently mentioned road/s as the main "via" road/s.
    4. If there is a tie in frequency, join the tied road names with a slash (e.g., "via Road A/Road B") to indicate multiple key roads in the route.

    Returns:
        A string in the format "via [road name]" or "via [road A]/[road B]" if multiple roads are tied as the main via roads.
        If no roads can be extracted, returns "via unknown".
    """

    road_pattern = re.compile(r"onto ([\w\s\.\-]+)")
    roads = []

    for step in steps:
        # Support both full ORS step objects and plain strings
        match = road_pattern.search(step)
        if match:
            road_name = match.group(1).strip()
            roads.append(road_name)

    if not roads:
        return "Unknown"

    counts = Counter(roads)
    max_count = max(counts.values())
    most_common = [road for road, c in counts.items() if c == max_count]
    limited_roads = most_common[:3]

    return "via " + "/".join(limited_roads)

async def sample_smartly(full_coords, turn_indices, limit=100):
    """
    Preserves turn indices and fills the remaining budget with points to maintain road geometry.

    Purpose:
    - Ensures that all critical turn points identified by OpenRouteService are included in the final set of waypoints.
    - Fills the remaining points up to the specified limit by selecting points that maintain the overall geometry of the route.

    Returns:
    - matching_coords: A list of coordinates that includes all turn points and additional points to fill up to the limit.
    - new_waypoint_indices: A list of indices indicating the positions of the original turn points
    """
    
    if len(full_coords) <= limit:
        return full_coords, list(range(len(full_coords)))

    # Use a set for unique indices to prevent errors
    protected_indices = sorted(list(set(turn_indices)))
    
    # Calculate how many points to add to fill the gaps
    remaining_budget = limit - len(protected_indices)
    
    # Trim turns if already over the limit
    if remaining_budget <= 0:
        final_indices = protected_indices[:limit]
        return [full_coords[i] for i in final_indices], list(range(len(final_indices)))

    # Fill gaps based on distance between protected points
    final_indices = list(protected_indices)
    
    # Fill the largest gaps first
    while len(final_indices) < limit:
        # Find the current largest gap between consecutive indices
        gaps = np.diff(final_indices)
        max_gap_idx = np.argmax(gaps)
        
        if gaps[max_gap_idx] <= 1:
            break
            
        # Add the midpoint of the largest gap
        new_idx = final_indices[max_gap_idx] + (gaps[max_gap_idx] // 2)
        final_indices.append(int(new_idx))
        final_indices.sort()

    # Map the original turn indices to their new positions in the 100-point list
    matching_coords = [full_coords[i] for i in final_indices]
    
    # Find where the original turns ended up in the new list
    new_waypoint_indices = []
    for turn_idx in protected_indices:
        try:
            new_pos = final_indices.index(turn_idx)
            new_waypoint_indices.append(new_pos)
        except ValueError:
            continue

    return matching_coords, new_waypoint_indices

class OpenRouteServiceAdapter(APIAdapter):
    """
    Adapter for OpenRouteService (ORS) providing Geocoding and Directions.
    """

    def __init__(self):
        self.client = httpx.AsyncClient()

        # API keys loaded from environment variables
        self.ors_api_key = os.getenv("ORS_API_KEY")
        self.geoapify_api_key = os.getenv("GEOAPIFY_API_KEY")

        # Endpoint URLs for Geoapify Geocoding, Reverse Geocoding, and OpenRouteService Directions
        self.geocode_url = "https://api.geoapify.com/v1/geocode/search"
        self.reverse_geocode_url = "https://api.geoapify.com/v1/geocode/reverse"
        self.directions_url = "https://api.openrouteservice.org/v2/directions/driving-car"

    # ---------- Geocoding ----------
    async def geocode(self, place_name: str) -> dict:
        """
        Geocodes a place name to latitude and longitude (coordinates).

        Returns:
            dict: {lat, lng}, if geocoding is successful
            None if geocoding fails or no results are found.
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
        Reverse geocodes latitude and longitude (coordinates)to a human-readable address.

        Returns:
            - str: A formatted address if reverse geocoding is successful
            - A fallback string in the format of "lat,lng" if reverse geocoding fails or no results are found.
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
    
    async def build_avoid_polygons(self, avoid_roads: List[str]):
        """
        Builds a combined MultiPolygon of avoid areas for specified roads.

        Returns:
           - a GeoJSON-compatible MultiPolygon coordinates list.
           - None: if no valid polygons available for any of the specified roads.
        """

        avoid_polygons_list = []

        for road in avoid_roads:
            # Load stored polygons first
            stored_poly = await load_polygons(road)

            if stored_poly:
                road_poly = stored_poly
            else:
                # Fallback to expensive computation
                road_poly = await get_road_polygon(self.client, road)

            if not road_poly:
                continue

            # Convert polygons to GeoJSON coordinates
            for poly in road_poly.geoms:
                coords = list(poly.exterior.coords)
                avoid_polygons_list.append([coords])

        if not avoid_polygons_list:
            return None

        return {
            "type": "MultiPolygon",
            "coordinates": avoid_polygons_list
        }

    # ---------- Directions ----------
    async def get_directions(self, 
                       origin: dict, 
                       destination: dict, 
                       option: str, 
                       via: List[dict] = None, 
                       avoid_roads: List[str] = None,
                       avoid_features: List[str] = None
                       ) -> List[dict]:
        """
        Fetch multiple routes from origin to destination.

        Returns:
            - List[dict]: routes in a format compatible with model.py
            - float: time taken for the ORS API call in milliseconds
        """

        coords = [[origin["lng"], origin["lat"]]]
        if via:
            coords.extend([[v["lng"], v["lat"]] for v in via])
        coords.append([destination["lng"], destination["lat"]])

        normalized_option = (option or "recommended").strip().lower()
        if normalized_option not in {"recommended", "fastest", "shortest"}:
            normalized_option = "recommended"

        payload = {
            "coordinates": coords,
            "instructions": True,
            "preference": normalized_option
        }

        # Request alternative routes if no waypoints included
        if not via:
            payload["alternative_routes"] = {
                "share_factor": 0.6,
                "target_count": 3,
                "weight_factor": 1.4
            }

        # Build avoid polygons
        avoid_polygons = None
        if avoid_roads:
            avoid_polygons = await self.build_avoid_polygons(avoid_roads)

        payload["options"] = {}
        if avoid_polygons:
            payload["options"]["avoid_polygons"] = avoid_polygons
        if avoid_features:
            payload["options"]["avoid_features"] = avoid_features

        headers = {
            "Authorization": self.ors_api_key,
            "Content-Type": "application/json"
        }
        start_time = time.perf_counter()
        resp = await self.client.post(self.directions_url, json=payload, headers=headers)
        end_time = time.perf_counter()
        ors_ms = (end_time - start_time) * 1000

        if resp.status_code != 200:
            error_detail = resp.text
            try:
                error_data = resp.json()
                if isinstance(error_data, dict):
                    error_obj = error_data.get("error", {})
                    error_detail = error_obj.get("message") or str(error_data)
                else:
                    error_detail = str(error_data)
            except Exception:
                pass
            raise RuntimeError(f"ORS directions failed ({resp.status_code}): {error_detail}")

        data = resp.json()

        routes_output = []
        for route in data.get("routes", []):
            # Decode the geometry to get all points
            encoded_geom = route.get("geometry")
            full_coords = []
            matching_coords = []
            mapbox_waypoints = []
            turn_indices = []

            # Decode polyline into coordinates
            if encoded_geom:
                # Decodes to (lat, lng) pairs; swap to (lng, lat) for consistency with Mapbox and ORS formats.
                decoded = polyline.decode(encoded_geom)
                full_coords = [[lng, lat] for lat, lng in decoded]

                # Identify the critical turn points from ORS indices
                turn_indices = [step["way_points"][0] for step in route["segments"][0]["steps"]]

                # Use the smart sampler
                matching_coords, mapbox_waypoints = await sample_smartly(full_coords, turn_indices, limit=100)

            steps_instructions = [step["instruction"] for step in route["segments"][0]["steps"]]
            via_road = await extract_via_road(steps_instructions)
            summary = route["summary"]

            routes_output.append({
                "via": via_road,
                "start": origin,
                "end": destination,
                "distance": f"{int(summary['distance']/1000)} km",
                "duration": f"{int(summary['duration']/60)} mins",
                "full_geometry": full_coords,
                "matching_coords": matching_coords,
                "waypoint_indices": mapbox_waypoints,
                "turn_indices": turn_indices,
                "steps_instructions": steps_instructions
            })
        return routes_output, ors_ms
