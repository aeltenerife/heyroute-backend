import json
import string
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from legacy_llm_gpt import process_with_gpt
from models import SavedPlace
from helpers import extract_json, check_label_role, format_heyroute_response
from prompts import INTENTS_PROMPT, NAVIGATION_INTENTS_PROMPT, PREFERENCE_INTENTS_PROMPT
from adapters.mapbox_directions_adapter import MapboxDirectionsAdapter

adapter = MapboxDirectionsAdapter()

async def detect_intent(latest_input, mode, conversation_history, semantic_context, user_id, session_id):
    """
    Detects user intent using GPT-based classification.

    Modes:
    - SETUP: Initial trip planning
    - NAVIGATION: Active navigation phase
    - PREFERENCE_CONFIRMATION: Preference handling

    Returns:
        - A dictionary of detected intents (boolean flags)
        - Latency of the intent detection step
    """
    check_intents_prompt = []
    if mode == 'PREFERENCE_CONFIRMATION':
        check_intents_prompt = [{'role': 'system', 'content': PREFERENCE_INTENTS_PROMPT}, {'role': 'user', 'content': f'The conversation so far:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in conversation_history)) + f'\n\nLatest user message: {latest_input}'}]
    elif mode == 'NAVIGATION':
        check_intents_prompt = [{'role': 'system', 'content': NAVIGATION_INTENTS_PROMPT}, {'role': 'user', 'content': f'The user is currently in navigation mode.\nThe conversation so far:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in conversation_history)) + f'\n\nLatest user message: {latest_input}'}]
    elif mode == 'SETUP':
        check_intents_prompt = [{'role': 'system', 'content': INTENTS_PROMPT}, {'role': 'system', 'content': f'Known semantic places for this user: {semantic_context}'}, {'role': 'user', 'content': f'The conversation so far:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in conversation_history)) + f'\n\nLatest user message: {latest_input}'}]
    else:
        pass
    raw_intents, intent_detect_latency = await process_with_gpt(check_intents_prompt)
    if raw_intents.startswith('HeyRoute:'):
        print(f'[INTENT] LLM returned error string: {raw_intents}')
        if mode == 'PREFERENCE_CONFIRMATION':
            return ({'preference_remembering': False, 'no_preference_remembering': False}, intent_detect_latency)
        elif mode == 'NAVIGATION':
            return ({'request_alternates': False, 'select_route': False, 'cancellation': False, 'start_new_trip': False}, intent_detect_latency)
        else:
            return ({k: False for k in ['clarifications', 'cancellation', 'generate_routes', 'trip_changes', 'start_nav', 'request_alternates', 'select_route']}, intent_detect_latency)
    else:
        pass
    try:
        import json_repair
        parsed = json_repair.loads(extract_json(raw_intents))
        print(f'[INTENT] Detected: {parsed}')
        return (parsed, intent_detect_latency)
    except Exception as e:
        print(f'[INTENT] JSON parse FAILED. Raw: {raw_intents[:500]}')
        if mode == 'PREFERENCE_CONFIRMATION':
            return ({'preference_remembering': False, 'no_preference_remembering': False}, intent_detect_latency)
        elif mode == 'NAVIGATION':
            return ({'request_alternates': False, 'select_route': False, 'cancellation': False, 'start_new_trip': False}, intent_detect_latency)
        else:
            return ({k: False for k in ['clarifications', 'cancellation', 'generate_routes', 'trip_changes', 'start_nav', 'request_alternates', 'select_route']}, intent_detect_latency)
    else:
        pass
    finally:
        pass

async def resolve_semantic_places(db: AsyncSession, user_input: str, semantic_context: dict, user_id: str):
    """
    Resolves semantic place references such as "home", "work", or "school".

    It detects labeled locations, retrieves stored coordinates if available, and updates semantic context to imrpove personalization.

    Returns:
        - Updated semantic context with resolved place labels and coordinates 
    """
    text = user_input.lower().translate(str.maketrans('', '', string.punctuation))
    
    stmt = select(SavedPlace).where(SavedPlace.user_id == user_id)
    result = await db.execute(stmt)
    saved_places_records = result.scalars().all()
    saved_places = {sp.label: {"lat": sp.latitude, "lng": sp.longitude} for sp in saved_places_records}

    for place, coords in saved_places.items():
        is_origin, is_destination = await check_label_role(text, place)
        if is_origin:
            semantic_context['origin_label'] = place
            semantic_context['origin_known'] = True
            semantic_context['origin_value'] = coords
        else:
            pass
        if is_destination:
            semantic_context['destination_label'] = place
            semantic_context['destination_known'] = True
            semantic_context['destination_value'] = coords
        else:
            pass
    else:
        pass
    return semantic_context

async def generate_route_and_response(user_id, session_id, origin, destination, option, via, avoid_roads, avoid_features, final_gpt_response, navigation_started, road=''):
    """
    Fetch routes, prepare summary, generate HeyRoute conversational response.

    Returns: 
        routes_data: List of route options
        primary_route: Selected main route
        response: Conversational output for the user
    """
    routes_data = []
    ors_latency = 0.0
    normalized_option = (option or 'recommended').strip().lower()
    if normalized_option not in {'recommended', 'fastest', 'shortest'}:
        normalized_option = 'recommended'
    else:
        pass
    attempts = [{'label': 'primary', 'via': via, 'avoid_roads': avoid_roads, 'avoid_features': avoid_features}]
    if avoid_roads or avoid_features:
        attempts.append({'label': 'retry_without_avoid', 'via': via, 'avoid_roads': [], 'avoid_features': []})
    else:
        pass
    if via:
        attempts.append({'label': 'retry_without_via', 'via': [], 'avoid_roads': [], 'avoid_features': []})
    else:
        pass
    for attempt in attempts:
        try:
            routes_data, ors_latency = await adapter.get_directions(origin=origin, destination=destination, option=normalized_option, via=attempt['via'], avoid_roads=attempt['avoid_roads'], avoid_features=attempt['avoid_features'])
        except Exception as e:
            response = "I'm sorry, I'm having trouble connecting to the routing service right now. Please try again later."
            return ([], {}, {'heyroute': response, 'ors_latency': ors_latency, 'user_id': user_id, 'session_id': session_id})
        else:
            pass
        finally:
            pass
        if routes_data:
            break
        else:
            pass
    else:
        pass
    if not routes_data:
        response = "I found the locations, but I couldn't find a drivable route between them."
        return ([], {}, {'heyroute': response, 'ors_latency': ors_latency, 'user_id': user_id, 'session_id': session_id})
    else:
        pass
    primary_route = routes_data[0] if routes_data else {}
    if road:
        for route in routes_data:
            if road in route.get('via', '').lower():
                primary_route = route
            else:
                pass
        else:
            pass
    else:
        pass
    alternatives = [r for r in routes_data if r != primary_route]
    origin = final_gpt_response.get('origin')
    destination = final_gpt_response.get('destination')
    route_summary = {'origin': origin, 'destination': destination, 'option': normalized_option, 'via': primary_route.get('via'), 'distance': primary_route.get('distance'), 'duration': primary_route.get('duration')}
    for route in routes_data:
        pass
    else:
        pass
    response = ''
    if not navigation_started:
        response = await format_heyroute_response(route_summary)
    else:
        pass
    if origin == 'current location':
        origin = 'Your location'
    else:
        pass
    return (routes_data, primary_route, {'heyroute': response, 'ors_latency': ors_latency, 'origin': origin, 'destination': destination, 'route_preview': True, 'route': primary_route, 'alternatives': alternatives, 'user_id': user_id, 'session_id': session_id})