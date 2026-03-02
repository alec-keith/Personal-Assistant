"""
Text-to-speech — cross-platform, male voice.

Primary: OpenAI TTS (tts-1-hd, onyx voice — deep, natural male, requires OPENAI_API_KEY).
Fallback: edge-tts (Microsoft Edge Neural TTS, free, no API key, works on Railway/Linux).
         Voice: en-US-ChristopherNeural — deep natural American male.
Final fallback: macOS built-in `say` with "Alex" (male voice, Mac only).

Output is an MP3/AIFF file playable via FFmpegPCMAudio in Discord voice channels.
"""

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

EDGE_VOICE = "en-US-ChristopherNeural"  # deep natural male voice
OPENAI_VOICE = "onyx"                   # deep natural male voice
OPENAI_MODEL = "tts-1"                  # fastest model, optimised for real-time voice


async def synthesize(text: str) -> Path | None:
    """
    Convert text to speech. Returns path to an audio file, or None on failure.
    Caller is responsible for deleting the file when done.
    """
    spoken = text[:400]

    # Primary: OpenAI TTS — most natural, requires OPENAI_API_KEY
    try:
        from config import settings
        if settings.openai_api_key:
            from openai import AsyncOpenAI  # type: ignore

            client = AsyncOpenAI(api_key=settings.openai_api_key)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                out_path = Path(f.name)

            response = await client.audio.speech.create(
                model=OPENAI_MODEL,
                voice=OPENAI_VOICE,
                input=spoken,
                response_format="mp3",
            )
            out_path.write_bytes(response.content)
            return out_path
    except ImportError:
        logger.debug("openai package not installed, falling back to edge-tts")
    except Exception:
        logger.warning("OpenAI TTS failed, falling back to edge-tts", exc_info=True)

    # Fallback: edge-tts — free, no API key, natural male voice, works on Linux
    try:
        import edge_tts  # type: ignore

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out_path = Path(f.name)

        communicate = edge_tts.Communicate(text=spoken, voice=EDGE_VOICE)
        await communicate.save(str(out_path))
        return out_path

    except ImportError:
        logger.debug("edge-tts not installed, trying macOS say")
    except Exception:
        logger.warning("edge-tts failed, trying macOS say", exc_info=True)

    # Final fallback: macOS say with Alex (male voice)
    try:
        import subprocess

        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
            out_path = Path(f.name)

        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["say", "-v", "Alex", "-o", str(out_path), spoken],
                check=True,
                capture_output=True,
            ),
        )
        return out_path

    except FileNotFoundError:
        logger.warning("No TTS available — set OPENAI_API_KEY or install edge-tts")
        return None
    except Exception:
        logger.exception("TTS synthesis failed")
        return None


def get_ffmpeg_exe() -> str:
    """Return path to bundled ffmpeg binary (from imageio-ffmpeg)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"
