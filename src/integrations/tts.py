"""
Text-to-speech using macOS built-in `say` command.

Produces AIFF files that can be played in Discord voice channels
via FFmpegPCMAudio (using the bundled imageio-ffmpeg binary).
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# macOS voice — Samantha is clear and natural. Others: Alex, Tom, Victoria, Allison
VOICE = "Samantha"


async def synthesize(text: str) -> Path | None:
    """
    Convert text to speech. Returns path to an AIFF file, or None on failure.
    Caller is responsible for deleting the file when done.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
            out_path = Path(f.name)

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["say", "-v", VOICE, "-o", str(out_path), text],
                check=True,
                capture_output=True,
            ),
        )
        return out_path
    except FileNotFoundError:
        logger.warning("macOS 'say' command not available")
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
        return "ffmpeg"  # fall back to system ffmpeg if available
