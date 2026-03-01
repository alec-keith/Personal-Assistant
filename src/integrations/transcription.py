"""
Audio transcription via Groq's Whisper API.

Free tier: 28 hours of audio per day, very fast.
Handles OGG, MP3, MP4, WAV, M4A, FLAC, WebM.
No local ffmpeg needed — Groq handles decoding server-side.
"""

import logging
from pathlib import Path

from groq import AsyncGroq

from config import settings

logger = logging.getLogger(__name__)

_client: AsyncGroq | None = None


def _get_client() -> AsyncGroq:
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=settings.groq_api_key)
    return _client


async def transcribe_bytes(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """
    Transcribe audio bytes using Groq Whisper.
    filename is used to hint the file format to the API.
    Returns the transcribed text, or empty string on failure.
    """
    if not settings.groq_api_key:
        logger.warning("GROQ_API_KEY not set — cannot transcribe audio")
        return ""

    try:
        client = _get_client()
        transcription = await client.audio.transcriptions.create(
            file=(filename, audio_bytes),
            model="whisper-large-v3-turbo",
            response_format="text",
        )
        text = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
        logger.info("Transcribed %d bytes → %d chars: %s", len(audio_bytes), len(text), text[:60])
        return text
    except Exception:
        logger.exception("Whisper transcription failed")
        return ""


async def transcribe_file(path: str | Path) -> str:
    """Transcribe an audio file from disk."""
    path = Path(path)
    return await transcribe_bytes(path.read_bytes(), path.name)
