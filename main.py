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
from db_utils import check_user_preferences, resolve_semantic_location
from sqlalchemy.future import select

from dotenv import load_dotenv
load_dotenv()

from vad_utils import apply_vad_filter
from llm_utils import process_with_llm
from prompts import SYSTEM_PROMPT, INTENTS_PROMPT

# --- State management for user sessions ---
class SessionState:
	def __init__ (self):
		self.conversation_history = []
		self.current_location = None # GPS
		self.semantic_context = {
			"origin_label": None, "destination_label": None, # Labels include work, home, school, etc.
			"origin_known": False, "destination_known": False,
			"origin_value": None, "destination_value": None # Actual coordinates/addresses
		}
SESSIONS = {}

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

	print(f"Processing audio file: {audio_file.filename} for user: {x_user_id}, session: {x_session_id}")
	
	try:
		# Read the uploaded audio file
		audio_bytes = await audio_file.read()

		# Run the raw audio through the VAD filter utility
		clean_audio_bytes = apply_vad_filter(audio_bytes)

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
			transcription_data = asr_response.json()
			user_text = transcription_data.get("text", "").strip()

		# --- LLM Intent Extraction ---
		# Initialize or load the user session state
		if x_session_id not in SESSIONS:
			SESSIONS[x_session_id] = SessionState()
		state = SESSIONS[x_session_id]

		if x_current_lat is not None and x_current_lng is not None:
			state.current_location = {"lat": x_current_lat, "lng": x_current_lng}

			# Debug
			print(f"Current location for session {x_session_id}: {state.current_location}")

		# Add the ASR transcription to the conversation history
		state.conversation_history.append({"role": "user", "content": user_text})

		# Intent parsing
		intent_prompt = [
			{"role": "system", "content": INTENTS_PROMPT},
			{"role": "system", "content": f"Known semantic places: {state.semantic_context}"},
			{"role": "user", "content": (
				"The conversation so far:\n" +
				"\n".join(f"{m['role']}: {m['content']}" for m in state.conversation_history)
				)}
		]
		detected_intents = await process_with_llm(intent_prompt)

		# Debug
		print(f"Detected intents for session {x_session_id}: {detected_intents}")

		final_travel_json = None

		if detected_intents.get("generate_routes") == True:
			extraction_prompt = [
				{"role": "system", "content": SYSTEM_PROMPT},
				{"role": "user", "content": (
					"The conversation so far is:\n" +
					"\n".join(f"{m['role']}: {m['content']}" for m in state.conversation_history) +
					"\n\nPlease output the final travel JSON now."
					)}
			]
			final_travel_json = await process_with_llm(extraction_prompt)

			# Debug
			print(f"Extracted travel data for session {x_session_id}: {final_travel_json}")

			state.final_llm_response = final_travel_json

		# --- Database preference querying ---
		resolved_destination_coords = None
		history_avoid = None
		history_familiar = None
		history_option = None

		if final_travel_json:
			destination_name = final_travel_json.get("destination", "").strip().lower()

			# Semantic location resolution is performed first to resolve keywords like "home", "work", etc.
			resolved_destination_coords = await resolve_semantic_location(
				db = db,
				user_id = x_user_id,
				destination_label = destination_name
			)

			if resolved_destination_coords:
				# Debug
				print(f"Semantic location matched, coordinates: {resolved_destination_coords}")

			history_avoid, history_familiar, history_option = await check_user_preferences(
				db = db,
				user_id = x_user_id,
				destination = final_travel_json.get("destination")
			)

			# Debug
			print(f"DB preferences - Avoid: {history_avoid}, Option: {history_option}, Familiar: {history_familiar}")
		
		return {
			"transcription": user_text,
			"intents": detected_intents,
			"travel_data": final_travel_json,
			"database_layer":{
				"semantic_resolved_coords": resolved_destination_coords,
				"historical_preferences": {
					"most_avoided_road": history_avoid,
					"most_familiar_road": history_familiar,
					"preferred_route_option": history_option
				}
			},
			"message": "Audio processed and transcribed, intents extracted, initial travel data generated, and database context retrieved."
		}

	except httpx.RequestError as exc:
		raise HTTPException(status_code=500, detail=f"Unable to connect to the ASR server: {str(exc)}")
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Unable to process the audio file: {str(e)}")

from routers.tts import router as tts_router
from routers.manual_nav import router as manual_nav_router
from routers.location import router as location_router
from routers.legacy_model import router as legacy_model_router

app.include_router(tts_router, prefix="/api/tts", tags=["TTS"])
app.include_router(manual_nav_router, prefix="/api/navigation", tags=["Manual Navigation"])
app.include_router(location_router, prefix="/api/location", tags=["Location"])
app.include_router(legacy_model_router, tags=["Legacy Model"])