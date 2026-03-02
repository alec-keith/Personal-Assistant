"""
Text-to-speech — cross-platform, male voice.

Primary: edge-tts (Microsoft Edge Neural TTS, free, no API key, works on Railway/Linux).
         Voice: en-US-ChristopherNeural — deep, natural American male.
Fallback: macOS built-in `say` with "Alex" (male voice, Mac only).

Output is an MP3/AIFF file playable via FFmpegPCMAudio in Discord voice channels.
"""

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

EDGE_VOICE = "en-US-ChristopherNeural"  # deep natural male voice


async def synthesize(text: str) -> Path | None:
    """
    Convert text to speech. Returns path to an audio file, or None on failure.
    Caller is responsible for deleting the file when done.
    """
    spoken = text[:400]

    # Primary: edge-tts — free, no API key, natural male voice, works on Linux
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

    # Fallback: macOS say with Alex (male voice)
    try:
        import subprocess

        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
            out_path = Path(f.name)

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["say", "-v", "Alex", "-o", str(out_path), spoken],
                check=True,
                capture_output=True,
            ),
        )
        return out_path

    except FileNotFoundError:
        logger.warning("No TTS available — install edge-tts: pip install edge-tts")
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
