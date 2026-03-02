"""
Text-to-speech — cross-platform.

Primary: gTTS (Google TTS, free, works on Railway/Linux, requires internet).
Fallback: macOS built-in `say` command (local, works offline, Mac only).

Output is an audio file (MP3 from gTTS, AIFF from say) playable via
FFmpegPCMAudio in Discord voice channels.
"""

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


async def synthesize(text: str) -> Path | None:
    """
    Convert text to speech. Returns path to an audio file, or None on failure.
    Caller is responsible for deleting the file when done.
    """
    spoken = text[:400]

    # Primary: gTTS — works on Linux/Railway (free, no key, needs internet)
    try:
        from gtts import gTTS  # type: ignore

        def _gtts_save(out: Path) -> None:
            tts = gTTS(text=spoken, lang="en", slow=False)
            tts.save(str(out))

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out_path = Path(f.name)

        await asyncio.get_event_loop().run_in_executor(None, _gtts_save, out_path)
        return out_path

    except ImportError:
        logger.debug("gTTS not installed, trying macOS say")
    except Exception:
        logger.warning("gTTS failed, trying macOS say", exc_info=True)

    # Fallback: macOS say (local, no internet)
    try:
        import subprocess

        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
            out_path = Path(f.name)

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["say", "-v", "Samantha", "-o", str(out_path), spoken],
                check=True,
                capture_output=True,
            ),
        )
        return out_path

    except FileNotFoundError:
        logger.warning("No TTS available — install gTTS: pip install gTTS")
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
