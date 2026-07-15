from fastapi import APIRouter, HTTPException, Header, Request
from session_manager import SESSIONS
from adapters.mapbox_directions_adapter import MapboxDirectionsAdapter

router = APIRouter()
adapter = MapboxDirectionsAdapter()

@router.post('/end_trip')
async def end_trip(session_id: str=Header(None, alias='X-Session-ID')):
    """
    Ends the current trip and clears the session state for the given session ID.
    """
    state = SESSIONS.get(session_id)
    if not state:
        raise HTTPException(status_code=404, detail='Session not found')
    state.clear_trip_context()
    return {'status': 'success', 'message': 'Trip context cleared'}

@router.post('/directions/reroute')
async def directions(request: Request, session_id: str=Header(None, alias='X-Session-ID')):
    """
    Generates updated routes based on the user's current location during navigation.
    """
    state = SESSIONS.get(session_id)
    if not state:
        raise HTTPException(status_code=404, detail='Session not found')
    
    payload = await request.json()
    new_lat = payload.get('lat')
    new_lng = payload.get('lng')
    if new_lat and new_lng:
        state.current_location = {'lat': new_lat, 'lng': new_lng}
    
    try:
        routes, ors_latency = await adapter.get_directions(
            origin=state.current_location, 
            destination=state.current_route_params.get('destination'), 
            option=state.current_route_params.get('option'), 
            via=state.current_route_params.get('via'), 
            avoid_roads=state.current_route_params.get('avoid_roads'), 
            avoid_features=state.current_route_params.get('avoid_features')
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Routing engine error: {str(e)}')
    
    if not routes:
        return {'error': 'No new routes found from current location'}
    
    primary_route = routes[0]
    alternatives = routes[1:]
    state.routes_data = routes
    state.primary_route = primary_route
    return {'primary_route': primary_route, 'alternatives': alternatives, 'ors_latency': ors_latency}
