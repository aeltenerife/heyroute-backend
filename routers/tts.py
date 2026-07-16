"""
This module implements the text-to-speech (TTS) functionality for HeyRoute.

Handles:
    - Converting text responses to speech using edge-tts
    - Streaming audio back to the client in MP3 format
"""

import io
import edge_tts
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

# Default TTS voice
VOICE = "en-US-AriaNeural"

class TTSRequest(BaseModel):
    text: str

@router.post("/speak")
async def speak(req: TTSRequest):
    """
    Endpoint to convert text to speech using edge-tts and return as streaming response.

    Returns an audio stream in MP3 format that can be played on the client side.
    """

    communicate = edge_tts.Communicate(text=req.text, voice=VOICE)

    async def generate():
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

    return StreamingResponse(generate(), media_type="audio/mpeg")
