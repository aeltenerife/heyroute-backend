import os
import io
import wave
import httpx
import json
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Query, Header
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from database import get_db
from db_utils import check_user_preferences
from sqlalchemy.future import select

from dotenv import load_dotenv
load_dotenv()

from vad_utils import apply_vad_filter

import asyncio
import time
from session_manager import SESSIONS, SessionState
from routing_engine import detect_intent, resolve_semantic_places, generate_route_and_response
from legacy_llm_gpt import process_with_gpt
from helpers import build_gpt_prompt, normalize_road_name, extract_json, toll_roads, resolve_collisions
from prompts import SYSTEM_PROMPT, CLARIFICATIONS_PROMPT, TRIP_CHANGES_PROMPT, SEMANTICS_PROMPT
from models import TripHistory
from datetime import datetime, timezone
from adapters.mapbox_directions_adapter import MapboxDirectionsAdapter
adapter = MapboxDirectionsAdapter()




# --- ASR server handoff configuration ---
ASR_URL = os.getenv("REMOTE_ASR_URL", "http://172.16.3.217:80").rstrip("/") + "/transcribe"
ASR_API_KEY = os.getenv("REMOTE_ASR_API_KEY")
if not ASR_API_KEY:
	raise ValueError("CRITICAL ERROR: REMOTE_ASR_API_KEY is missing from the environment variables.")

app = FastAPI(title="HeyRoute API")

@app.get("/health")
async def health_check():
	return {"status": "online", "message": "The HeyRoute FastAPI server is running!"}

@app.get("/db-check")
async def db_check(db: AsyncSession = Depends(get_db)):
	try:
		await db.execute(text("SELECT 1"))
		return {"database_status": "connected", "message": "Successfully communicating with postgresql"}
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")

# --- Voice processing endpoint ---
# This endpoint handles the flow of audio file processing, namely:
# 1. App sends audio file to this endpoint (DONE)
# 2. This endpoint applies VAD to clean the audio (DONE)
# 3. This endpoint sends the cleaned audio to the ASR server for transcription (DONE)
# 4. This endpoint receives the transcription from the ASR server (DONE)
# 5. This endpoint sends the transcription to the LLM server for intent extraction (TODO)
# 6. This endpoint receives the intent from the LLM server (TODO)
# 7. This endpoint sends the transcription back to the LLM for further processing (destination, preferences, etc.) (TODO)
# *. This endpoint receives the final JSON navigation payload from the LLM server (TODO)
# 7. This endpoint sends the JSON navigation payload back to the App (TODO)

@app.post("/api/voice/vad")
async def process_voice_activity(
	audio_file: UploadFile = File(...), 
	download: bool = Query(False, description="Set to true to download the cleaned audio file"),
	x_user_id: str = Header(..., description="User ID for database querying"),
	x_session_id: str = Header(..., description="Session ID for the current routing trip"),
	x_current_lat: float = Header(None, description="Current latitude"),
	x_current_lng: float = Header(None, description="Current longitude"),
	db: AsyncSession = Depends(get_db)
	):

	user_id = x_user_id
	session_id = x_session_id
	print(f"Processing audio file: {audio_file.filename} for user: {user_id}, session: {session_id}")
	
	try:
		# Read the uploaded audio file
		audio_bytes = await audio_file.read()

		# Decode the audio to 16kHz Mono PCM using pydub
		try:
			from pydub import AudioSegment
			audio_segment = AudioSegment.from_file(io.BytesIO(audio_bytes))
			audio_segment = audio_segment.set_frame_rate(16000).set_channels(1).set_sample_width(2)
			pcm_data = audio_segment.raw_data
		except Exception as e:
			raise HTTPException(status_code=400, detail=f"Failed to decode audio: {str(e)}")

		# Run the raw PCM audio through the VAD filter utility
		clean_audio_bytes = apply_vad_filter(pcm_data, sample_rate=16000)

		# Convert the cleaned audio bytes to a WAV format for ASR processing
		wav_io = io.BytesIO()
		with wave.open(wav_io, 'wb') as wav_file:
			wav_file.setnchannels(1)  # Mono
			wav_file.setsampwidth(2)  # 16-bit samples
			wav_file.setframerate(16000)  # 16 kHz sample rate
			wav_file.writeframes(clean_audio_bytes)

		# Reset the BytesIO stream position to the beginning for reading
		wav_io.seek(0)

		# If the user requested to download the cleaned audio, return it as a response (this skips the ASR step)
		if download:
			return Response(
				content = wav_io.getvalue(),
				media_type = "audio/wav",
				headers = {"Content-Disposition": f"attachment; filename=clean_{audio_file.filename}.wav"}
			)
		
		# Send the cleaned audio to the ASR server for transcription
		async with httpx.AsyncClient(timeout=30.0) as client:
			files = {'file': (f"clean_{audio_file.filename}.wav", wav_io, "audio/wav")}
			headers = {'X-API-Key': ASR_API_KEY}

			asr_response = await client.post(ASR_URL, files=files, headers=headers)

			# Check if the ASR server responded with an error
			if asr_response.status_code != 200:
				raise HTTPException(status_code = asr_response.status_code,
									detail = f"ASR Server Error: {asr_response.text}"
				)
			
			# Extract the transcription data from the ASR server's response
			try:
				transcription_data = asr_response.json()
				user_text = transcription_data.get("text", "").strip()
			except Exception:
				import json_repair
				transcription_data = json_repair.loads(asr_response.text)
				if isinstance(transcription_data, dict):
					user_text = transcription_data.get("text", "").strip()
				else:
					raise HTTPException(status_code=500, detail=f"ASR Server returned invalid format: {asr_response.text}")

		# --- LLM Intent Extraction ---
		# Initialize or load the user session state
		if x_session_id not in SESSIONS:
			SESSIONS[x_session_id] = SessionState()
		state = SESSIONS[x_session_id]

		if x_current_lat is not None and x_current_lng is not None:
			state.current_location = {"lat": x_current_lat, "lng": x_current_lng}

			# Debug
			print(f"Current location for session {x_session_id}: {state.current_location}")


		current_turn = state.increment_turn()
		user_input = user_text
		if not user_input:
			return {'heyroute': "I didn't catch that. Could you say it again?", 'history': state.conversation_history, 'turn_number': current_turn, 'intents': {}}
		else:
			pass
		state.conversation_history.append({'role': 'user', 'content': user_input})
		state.semantic_context = await resolve_semantic_places(db, user_input, state.semantic_context, x_user_id)
		intent_mode = ''
		if state.pending_preference and state.final_gpt_response:
			intent_mode = 'PREFERENCE_CONFIRMATION'
		elif state.navigation_started:
			intent_mode = 'NAVIGATION'
		else:
			intent_mode = 'SETUP'
		intents, intent_detect_latency = await detect_intent(user_input, intent_mode, state.conversation_history, state.semantic_context, x_user_id, x_session_id)
		intents = await resolve_collisions(intents)
		if intents.get('clarifications'):
			prompt = await build_gpt_prompt(SYSTEM_PROMPT, CLARIFICATIONS_PROMPT, f'The conversation so far:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in state.conversation_history)) + f'\n\nLatest user message: {user_input}')
			response, gpt_latency = await process_with_gpt(prompt)
			state.conversation_history.append({'role': 'assistant', 'content': response})
			return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'gpt_latency': gpt_latency, 'user_id': user_id, 'session_id': session_id}
		elif intents.get('cancellation'):
			response = "Okay, I've cancelled your trip. Let me know if you'd like to start a new one later."
			state.conversation_history.append({'role': 'assistant', 'content': response})
			state.clear_trip_context()
			return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'user_id': user_id, 'session_id': session_id, 'navigation_started': False}
		elif intents.get('generate_routes') or intents.get('trip_changes'):
			final_response = ''
			if intents.get('trip_changes'):
				if state.semantic_context['destination_known']:
					final_json_prompt = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'system', 'content': TRIP_CHANGES_PROMPT}, {'role': 'system', 'content': SEMANTICS_PROMPT}, {'role': 'user', 'content': 'The conversation so far is:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in state.conversation_history)) + f"\n\ndestination_known = {state.semantic_context['destination_known']}" + '\n\nPlease output the final travel JSON now.'}]
				else:
					final_json_prompt = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'system', 'content': TRIP_CHANGES_PROMPT}, {'role': 'user', 'content': 'The conversation so far is:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in state.conversation_history)) + '\n\nPlease output the final travel JSON now.'}]
				final_response, final_json_latency = await process_with_gpt(final_json_prompt)
			else:
				if state.semantic_context['destination_known']:
					final_json_prompt = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'system', 'content': SEMANTICS_PROMPT}, {'role': 'user', 'content': 'The conversation so far is:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in state.conversation_history)) + f"\n\ndestination_known = {state.semantic_context['destination_known']}" + '\n\nPlease output the final travel JSON now.'}]
				else:
					final_json_prompt = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': 'The conversation so far is:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in state.conversation_history)) + '\n\nPlease output the final travel JSON now.'}]
				final_response, final_json_latency = await process_with_gpt(final_json_prompt)
			try:
				state.final_gpt_response = json.loads(extract_json(final_response))
				origin_coords = None
				destination_coords = None
				destination = None
				tasks = []
				task_mapping = {}
				if state.semantic_context['origin_known']:
					origin_coords = state.semantic_context['origin_value']
					state.final_gpt_response['origin'] = state.semantic_context['origin_label']
				else:
					origin = state.final_gpt_response.get('origin')
					if origin == 'current location' and state.current_location:
						origin_coords = state.current_location
					else:
						task_mapping['origin'] = len(tasks)
						tasks.append(adapter.geocode(origin))
				if state.semantic_context['destination_known']:
					destination_coords = state.semantic_context['destination_value']
					state.final_gpt_response['destination'] = state.semantic_context['destination_label']
					destination = state.final_gpt_response['destination']
				else:
					destination = state.final_gpt_response['destination']
					task_mapping['destination'] = len(tasks)
					tasks.append(adapter.geocode(destination))
				via_coords = []
				via_input = state.final_gpt_response.get('via')
				if via_input:
					task_mapping['via_start'] = len(tasks)
					for place in via_input:
						if place.strip():
							tasks.append(adapter.geocode(place.strip()))
						else:
							pass
					else:
						pass
				else:
					pass
				start_time = time.perf_counter()
				results = await asyncio.gather(*tasks)
				end_time = time.perf_counter()
				geocode_latency = (end_time - start_time) * 1000
				if 'origin' in task_mapping:
					origin_coords = results[task_mapping['origin']]
				else:
					pass
				if 'destination' in task_mapping:
					destination_coords = results[task_mapping['destination']]
				else:
					pass
				if 'via_start' in task_mapping:
					via_coords = [r for r in results[task_mapping['via_start']:] if r]
				else:
					pass
				if not origin_coords or not destination_coords:
					response = "I couldn't find your destination. Could you be more specific?"
					state.conversation_history.append({'role': 'assistant', 'content': response})
					return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'final_json_latency': final_json_latency, 'user_id': user_id, 'session_id': session_id}
				else:
					pass
				avoid_roads = []
				avoid_features = []
				avoid_input = state.final_gpt_response.get('avoid')
				if avoid_input:
					for item in avoid_input:
						item = item.strip().lower()
						if item in ['highways', 'tollways', 'ferries']:
							avoid_features.append(item)
						else:
							normalized_road = await normalize_road_name(item)
							if normalized_road in toll_roads:
								avoid_features.append('tollways')
							else:
								avoid_roads.append(normalized_road)
					else:
						pass
				else:
					pass
				avoid_features = list(set(avoid_features))
				route_option = state.final_gpt_response.get('option', 'recommended')
				most_avoided_road, most_used_road, most_preferred_option = await check_user_preferences(db, x_user_id, destination)
				state.current_route_params = {'origin': origin_coords, 'destination': destination_coords, 'option': route_option, 'via': via_coords, 'avoid_roads': avoid_roads, 'avoid_features': avoid_features}
				if state.pending_preference:
					if most_preferred_option:
						state.pending_preference = 'route_option'
						response = f'You usually take the {most_preferred_option} route for this trip. Do you want me to use the {most_preferred_option} route?'
						state.conversation_history.append({'role': 'assistant', 'content': response})
						return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'final_json_latency': final_json_latency, 'geocode_latency': geocode_latency, 'user_id': user_id, 'session_id': session_id}
					elif not avoid_input and most_avoided_road:
						state.pending_preference = 'avoid'
						response = f'You usually avoid {most_avoided_road} on this trip. Do you want me to avoid it again?'
						state.conversation_history.append({'role': 'assistant', 'content': response})
						return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'final_json_latency': final_json_latency, 'geocode_latency': geocode_latency, 'user_id': user_id, 'session_id': session_id}
					elif most_used_road:
						state.pending_preference = 'familiarity'
						response = f'You usually take the route {most_used_road}. Do you want me to use it again?'
						state.conversation_history.append({'role': 'assistant', 'content': response})
						return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'final_json_latency': final_json_latency, 'geocode_latency': geocode_latency, 'user_id': user_id, 'session_id': session_id}
					else:
						pass
				else:
					pass
				state.routes_data, state.primary_route, response = await generate_route_and_response(x_user_id, x_session_id, origin_coords, destination_coords, route_option, via_coords, avoid_roads, avoid_features, state.final_gpt_response, state.navigation_started)
				state.route_created = True
				state.pending_preference = False
				state.conversation_history.append({'role': 'assistant', 'content': response.get('heyroute')})
				response['final_json_latency'] = final_json_latency
				response['geocode_latency'] = geocode_latency
				response['intent_detect_latency'] = intent_detect_latency
				response['history'] = state.conversation_history
				response['turn_number'] = current_turn
				response['intents'] = intents
				return response
			except (json.JSONDecodeError, TypeError) as e:
				response = "I'm sorry, I couldn't understand the travel details. Could you please clarify?"
				state.conversation_history.append({'role': 'assistant', 'content': response})
				return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'final_json_latency': final_json_latency, 'user_id': user_id, 'session_id': session_id}
			else:
				pass
			finally:
				pass
		elif intents.get('preference_remembering'):
			road = ''
			params = state.current_route_params
			destination = None
			preference_value = None
			if state.semantic_context['destination_known']:
				destination = state.semantic_context['destination_label']
			else:
				destination = state.final_gpt_response['destination']
			most_avoided_road, most_used_road, most_preferred_option = await check_user_preferences(db, x_user_id, destination)
			if state.pending_preference == 'route_option':
				params['option'] = most_preferred_option
				preference_value = most_preferred_option
				state.current_route_params['option'] = most_preferred_option
			elif state.pending_preference == 'avoid':
				preference_value = most_avoided_road
				if most_avoided_road in toll_roads:
					params['avoid_features'].append('tollways')
					state.current_route_params['avoid_features'].append('tollways')
				else:
					params['avoid_roads'].append(most_avoided_road)
					state.current_route_params['avoid_roads'].append(most_avoided_road)
			elif state.pending_preference == 'familiarity':
				preference_value = most_used_road
				road = most_used_road
			else:
				pass
			state.routes_data, state.primary_route, response = await generate_route_and_response(x_user_id, x_session_id, params['origin'], params['destination'], params['option'], params['via'], params['avoid_roads'], params['avoid_features'], state.final_gpt_response, state.navigation_started, road)
			state.route_created = True
			state.conversation_history.append({'role': 'assistant', 'content': response.get('heyroute')})
			response['intent_detect_latency'] = intent_detect_latency
			response['history'] = state.conversation_history
			response['turn_number'] = current_turn
			response['intents'] = intents
			state.pending_preference = False
			return response
		elif intents.get('no_preference_remembering'):
			params = state.current_route_params
			destination = None
			if state.semantic_context['destination_known']:
				destination = state.semantic_context['destination_label']
			else:
				destination = state.final_gpt_response['destination']
			most_avoided_road, most_used_road, most_preferred_option = await check_user_preferences(db, x_user_id, destination)
			if state.pending_preference == 'route_option':
				preference_value = most_preferred_option
			elif state.pending_preference == 'avoid':
				preference_value = most_avoided_road
			elif state.pending_preference == 'familiarity':
				preference_value = most_used_road
			else:
				pass
			state.routes_data, state.primary_route, response = await generate_route_and_response(x_user_id, x_session_id, params['origin'], params['destination'], params['option'], params['via'], params['avoid_roads'], params['avoid_features'], state.final_gpt_response, state.navigation_started)
			state.route_created = True
			state.conversation_history.append({'role': 'assistant', 'content': response.get('heyroute')})
			response['intent_detect_latency'] = intent_detect_latency
			response['history'] = state.conversation_history
			response['turn_number'] = current_turn
			response['intents'] = intents
			state.pending_preference = False
			return response
		elif intents.get('start_nav'):
			if state.route_created and state.primary_route:
				origin = state.final_gpt_response.get('origin')
				destination = state.final_gpt_response['destination']
				major_road = state.primary_route.get('via', '')
				avoid_list = state.final_gpt_response.get('avoid', [])
				route_option = state.current_route_params['option']
				primary_route = state.primary_route
				alternatives = [r for r in state.routes_data if r != state.primary_route]
				if route_option != 'recommended':
					pass
				else:
					pass
				if avoid_list:
					pass
				else:
					pass
				response = f'Starting navigation to {destination} now. Safe travels!'
				
				new_trip = TripHistory(
					user_id=user_id,
					origin_coords=state.current_route_params["origin"],
					destination_coords=state.current_route_params["destination"],
					via_road_name=major_road,
					route_option=route_option,
					avoid_roads=avoid_list,
					origin_name=origin,
					destination_name=destination,
					created_at=datetime.now(timezone.utc).replace(tzinfo=None)
				)
				db.add(new_trip)
				await db.commit()

				state.navigation_started = True
				state.conversation_history.append({'role': 'assistant', 'content': response})
				return {'heyroute': response, 'preferences': {'route_option': route_option, 'avoid_list': avoid_list, 'major_road': major_road}, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'navigation_started': True, 'route': primary_route, 'alternatives': alternatives, 'user_id': user_id, 'session_id': session_id}
			else:
				response = "I still don't have enough details to start navigation."
				state.conversation_history.append({'role': 'assistant', 'content': response})
				return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'user_id': user_id, 'session_id': session_id}
		elif intents.get('request_alternates') and state.routes_data:
			if state.navigation_started:
				params = state.current_route_params
				state.routes_data, state.primary_route, response = await generate_route_and_response(x_user_id, x_session_id, state.current_location, params['destination'], params['option'], params['via'], params['avoid_roads'], params['avoid_features'], state.final_gpt_response, state.navigation_started)
			else:
				pass
			route_summaries = []
			for idx, route in enumerate(state.routes_data, start=1):
				route_summaries.append({'index': idx, 'via': route.get('via'), 'distance': route.get('distance'), 'duration': route.get('duration')})
			else:
				pass
			response = await format_alternates_response(route_summaries)
			state.conversation_history.append({'role': 'assistant', 'content': response})
			return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'user_id': user_id, 'session_id': session_id}
		elif intents.get('select_route') and state.routes_data:
			select_route_prompt = [{'role': 'system', 'content': 'Identify if the user selected a route among alternates.'}, {'role': 'user', 'content': f'The conversation so far:\n' + '\n'.join((f"User: {m['content']}" if m['role'] == 'user' else f"Assistant: {m['content']}" for m in state.conversation_history[1:])) + f'\n\nLatest user message: {user_input}\n\nRespond strictly in JSON as:\n{{ "route_select": <number> or null }}'}]
			route_select_raw, gpt_latency = await process_with_gpt(select_route_prompt)
			try:
				route_select_data = json.loads(extract_json(route_select_raw))
				selected_index = route_select_data.get('route_select')
			except:
				selected_index = None
			else:
				pass
			finally:
				pass
			if selected_index and isinstance(selected_index, int) and (1 <= selected_index <= len(state.routes_data)):
				selected_route = state.routes_data[selected_index - 1]
				state.primary_route = selected_route
				alternatives = [r for r in state.routes_data if r != state.primary_route]
				if not state.navigation_started:
					route_summary = {'origin': state.final_gpt_response['origin'], 'destination': state.final_gpt_response['destination'], 'option': state.current_route_params['option'], 'via': selected_route.get('via'), 'distance': selected_route.get('distance'), 'duration': selected_route.get('duration')}
					response = await format_heyroute_response(route_summary)
					state.conversation_history.append({'role': 'assistant', 'content': response})
					return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'gpt_latency': gpt_latency, 'route_preview': True, 'route': state.primary_route, 'alternatives': alternatives, 'user_id': user_id, 'session_id': session_id}
				else:
					response = 'Switching to the selected route now.'
					state.conversation_history.append({'role': 'assistant', 'content': response})
					return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'navigation_started': True, 'switch_route': True, 'route': state.primary_route, 'alternatives': alternatives}
			else:
				response = "I couldn't understand which route you selected. Please specify the route number clearly."
				state.conversation_history.append({'role': 'assistant', 'content': response})
				return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'user_id': user_id, 'session_id': session_id}
		elif intents.get('start_new_trip'):
			response = 'To start a new trip, please cancel the current one first. Do you want me to cancel the current trip?'
			state.conversation_history.append({'role': 'assistant', 'content': response})
			return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'user_id': user_id, 'session_id': session_id}
		else:
			pass
		response = "I'm sorry, I don't know that one."
		state.conversation_history.append({'role': 'assistant', 'content': response})
		return {'heyroute': response, 'history': state.conversation_history, 'turn_number': current_turn, 'intents': intents, 'intent_detect_latency': intent_detect_latency, 'user_id': user_id, 'session_id': session_id}

	except httpx.RequestError as exc:
		raise HTTPException(status_code=500, detail=f"Unable to connect to the ASR server: {str(exc)}")
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Unable to process the audio file: {str(e)}")

from routers.tts import router as tts_router
from routers.manual_nav import router as manual_nav_router
from routers.location import router as location_router
from routers.trip import router as trip_router
from routers.history import router as history_router
from routers.places import router as places_router

app.include_router(tts_router, prefix="/api/tts", tags=["TTS"])
app.include_router(manual_nav_router, prefix="/api/navigation", tags=["Manual Navigation"])
app.include_router(location_router, prefix="/api/location", tags=["Location"])
app.include_router(trip_router, tags=["Trip Management"])
app.include_router(history_router, prefix="/api/history", tags=["Trip History"])
app.include_router(places_router, prefix="/api/places", tags=["Saved Places"])