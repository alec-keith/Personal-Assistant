"""
Discord gateway — DM + voice support.

Text DMs: normal chat with Roman.
Voice message / audio attachments: transcribed via Groq Whisper, handled as text.
Voice channel: Roman auto-joins when you enter a voice channel, auto-leaves when you leave.
               Listens for speech via SilenceDetectingSink, transcribes with Whisper,
               and speaks replies via TTS.
"""

import array
import asyncio
import io
import logging
import math
import wave
from typing import Callable, Awaitable

import discord

from config import settings
from .base import MessagingGateway
from src.integrations.transcription import transcribe_bytes
from src.integrations.tts import synthesize, get_ffmpeg_exe

logger = logging.getLogger(__name__)

AUDIO_TYPES = {
    "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav",
    "audio/x-m4a", "audio/aiff", "audio/webm", "video/mp4",
}

IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
}

JOIN_VOICE_CMDS = {"join voice", "join call", "/voice", "/join"}
LEAVE_VOICE_CMDS = {"leave voice", "leave call", "/leave", "/disconnect"}

# Minimum RMS energy to consider PCM audio as speech (not silence)
SPEECH_ENERGY_THRESHOLD = 400


# ------------------------------------------------------------------
# Voice capture sink
# ------------------------------------------------------------------

class SilenceDetectingSink(discord.sinks.AudioSink):
    """
    Captures raw PCM audio from Discord voice for a single target user.
    After SILENCE_SECS of no audio packets the utterance is fire via on_utterance(wav_bytes).

    Thread model: write() is called from discord.py's audio receive thread.
    Data is pushed to an asyncio.Queue via call_soon_threadsafe so the event
    loop handles processing — no locking needed.
    """

    SILENCE_SECS = 1.5   # seconds of no packets → end of utterance
    MIN_SECS = 0.4       # minimum utterance length (ignores blips)
    SAMPLE_RATE = 48000  # Discord native rate
    CHANNELS = 2         # stereo
    SAMPLE_WIDTH = 2     # 16-bit PCM

    def __init__(self, target_user_id: int, loop: asyncio.AbstractEventLoop) -> None:
        self.target_user_id = target_user_id
        self._loop = loop
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    @property
    def wants_opus(self) -> bool:
        return False  # request decoded PCM

    def write(self, data, user) -> None:
        if user.id != self.target_user_id:
            return
        # Thread-safe push to event loop
        self._loop.call_soon_threadsafe(self._queue.put_nowait, bytes(data.data))

    def cleanup(self) -> None:
        # Signal listen() to exit
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def listen(self, on_utterance: Callable[[bytes], Awaitable[None]]) -> None:
        """
        Runs on the event loop. Uses asyncio.wait_for timeout as a silence detector:
        when no audio packets arrive for SILENCE_SECS, the buffer is processed.
        """
        min_bytes = int(self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH * self.MIN_SECS)
        buffer = bytearray()

        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._queue.get(), timeout=self.SILENCE_SECS
                )
                if chunk is None:  # cleanup signal
                    break
                buffer.extend(chunk)

            except asyncio.TimeoutError:
                if len(buffer) >= min_bytes:
                    pcm = bytes(buffer)
                    buffer = bytearray()
                    if _has_speech(pcm):
                        wav = _pcm_to_wav(
                            pcm, self.SAMPLE_RATE, self.CHANNELS, self.SAMPLE_WIDTH
                        )
                        await on_utterance(wav)
                elif buffer:
                    buffer = bytearray()  # too short, discard


# ------------------------------------------------------------------
# PCM helpers
# ------------------------------------------------------------------

def _pcm_to_wav(pcm: bytes, rate: int, channels: int, width: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _has_speech(pcm: bytes, threshold: float = SPEECH_ENERGY_THRESHOLD) -> bool:
    """Return True if PCM contains audible speech (not silence)."""
    if len(pcm) < 2:
        return False
    try:
        samples = array.array("h", pcm)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        return rms > threshold
    except Exception:
        return True  # assume speech on error


# ------------------------------------------------------------------
# Gateway
# ------------------------------------------------------------------

class DiscordGateway(MessagingGateway):
    def __init__(self, on_message: Callable[..., Awaitable[str]]) -> None:
        self._on_message = on_message
        self._dm_channel: discord.DMChannel | None = None
        self._text_channel: discord.TextChannel | None = None
        self._voice_client: discord.VoiceClient | None = None
        self._speak_in_voice: bool = False
        self._sink: SilenceDetectingSink | None = None
        self._listen_task: asyncio.Task | None = None
        self._is_busy: bool = False  # prevent overlapping responses

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.voice_states = True
        intents.guilds = True

        self._client = discord.Client(intents=intents)
        self._register_events()

    # ------------------------------------------------------------------
    # Event registration
    # ------------------------------------------------------------------

    def _register_events(self) -> None:
        client = self._client

        @client.event
        async def on_ready() -> None:
            logger.info("Discord gateway online as %s", client.user)
            user = await client.fetch_user(settings.discord_user_id)
            self._dm_channel = await user.create_dm()
            logger.info("DM channel ready with user %s", user.name)
            if settings.discord_channel_id:
                ch = client.get_channel(settings.discord_channel_id)
                if ch is None:
                    ch = await client.fetch_channel(settings.discord_channel_id)
                self._text_channel = ch
                logger.info("Server text channel ready: #%s", ch.name)

        @client.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            if member.id != settings.discord_user_id:
                return

            joined = before.channel is None and after.channel is not None
            left = before.channel is not None and after.channel is None
            moved = (
                before.channel is not None
                and after.channel is not None
                and before.channel.id != after.channel.id
            )

            if joined or moved:
                await self._stop_voice_listening()
                if self._voice_client and self._voice_client.is_connected():
                    await self._voice_client.disconnect(force=True)

                target = after.channel
                try:
                    self._voice_client = await target.connect()
                    self._speak_in_voice = True
                    await self._start_voice_listening()
                    logger.info("Joined voice channel: %s", target.name)
                except Exception:
                    logger.exception("Failed to join voice channel %s", target.name)

            elif left:
                self._speak_in_voice = False
                await self._stop_voice_listening()
                if self._voice_client and self._voice_client.is_connected():
                    await self._voice_client.disconnect(force=True)
                self._voice_client = None
                logger.info("Left voice channel")

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.id != settings.discord_user_id:
                return
            if message.author.bot:
                return
            in_dm = isinstance(message.channel, discord.DMChannel)
            in_text_channel = (
                settings.discord_channel_id != 0
                and message.channel.id == settings.discord_channel_id
            )
            if not in_dm and not in_text_channel:
                return

            cmd = message.content.strip().lower()

            if cmd in JOIN_VOICE_CMDS:
                await self._handle_join_voice(message)
                return

            if cmd in LEAVE_VOICE_CMDS:
                await self._handle_leave_voice(message)
                return

            # Attachments — audio takes priority, then images
            if message.attachments:
                for attachment in message.attachments:
                    ct = (attachment.content_type or "").split(";")[0].strip().lower()
                    if ct in AUDIO_TYPES or _is_voice_message(message):
                        asyncio.create_task(self._handle_audio(message, attachment))
                        return

                image_attachments = [
                    a for a in message.attachments
                    if (a.content_type or "").split(";")[0].strip().lower() in IMAGE_TYPES
                ]
                if image_attachments:
                    asyncio.create_task(
                        self._handle_images(message, image_attachments)
                    )
                    return

            # Regular text message
            user_text = message.content.strip()
            if not user_text:
                return

            logger.info("Received text: %s", user_text[:80])
            async with message.channel.typing():
                try:
                    response = await self._on_message(user_text)
                    for chunk in _chunk(response, 1900):
                        await message.channel.send(chunk)
                    if self._speak_in_voice and self._voice_client:
                        asyncio.create_task(self._play_tts(response))
                except Exception:
                    logger.exception("Error processing text message")
                    await message.channel.send("Hit an error. Check the logs.")

    # ------------------------------------------------------------------
    # Voice listening
    # ------------------------------------------------------------------

    async def _start_voice_listening(self) -> None:
        if not self._voice_client or not self._voice_client.is_connected():
            return

        loop = asyncio.get_event_loop()
        self._sink = SilenceDetectingSink(settings.discord_user_id, loop)
        self._listen_task = asyncio.create_task(
            self._sink.listen(self._on_voice_utterance)
        )

        try:
            self._voice_client.start_recording(self._sink, self._recording_done)
            logger.info("Voice listening started")
        except Exception:
            logger.exception("Failed to start voice recording")
            self._listen_task.cancel()
            self._listen_task = None
            self._sink = None

    async def _stop_voice_listening(self) -> None:
        if self._voice_client and self._voice_client.is_connected():
            try:
                self._voice_client.stop_recording()
            except Exception:
                pass  # not recording — that's fine

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await asyncio.shield(self._listen_task)
            except (asyncio.CancelledError, Exception):
                pass

        self._listen_task = None
        self._sink = None

    async def _recording_done(self, sink, *args) -> None:
        """Called by discord.py when stop_recording() fires. No-op."""
        pass

    async def _on_voice_utterance(self, wav_bytes: bytes) -> None:
        """Called by the listen loop when the user finishes speaking."""
        if self._is_busy:
            return

        self._is_busy = True
        try:
            text = await transcribe_bytes(wav_bytes, "voice.wav")
            if not text or len(text.strip()) < 3:
                return

            logger.info("Voice utterance: %s", text[:80])

            # Echo transcription to text channel
            channel = self._text_channel or self._dm_channel
            if channel:
                await channel.send(f"🎙 _{text}_")

            response = await self._on_message(text)

            if channel:
                for chunk in _chunk(response, 1900):
                    await channel.send(chunk)

            await self._play_tts(response)

        except Exception:
            logger.exception("Voice utterance handling failed")
        finally:
            self._is_busy = False

    # ------------------------------------------------------------------
    # Audio attachment handler
    # ------------------------------------------------------------------

    async def _handle_audio(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> None:
        try:
            await message.channel.send("🎙 Transcribing...")
            audio_bytes = await attachment.read()
            text = await transcribe_bytes(audio_bytes, attachment.filename or "audio.ogg")

            if not text:
                await message.channel.send(
                    "Couldn't transcribe that — try again or type it out."
                )
                return

            logger.info("Voice memo transcribed: %s", text[:80])
            await message.channel.send(f'_"{text}"_')

            async with message.channel.typing():
                response = await self._on_message(text)
                for chunk in _chunk(response, 1900):
                    await message.channel.send(chunk)
                if self._speak_in_voice and self._voice_client:
                    asyncio.create_task(self._play_tts(response))
        except Exception:
            logger.exception("Error handling audio attachment")

    # ------------------------------------------------------------------
    # Image attachment handler
    # ------------------------------------------------------------------

    async def _handle_images(
        self,
        message: discord.Message,
        attachments: list[discord.Attachment],
    ) -> None:
        try:
            images: list[tuple[bytes, str]] = []
            for attachment in attachments:
                media_type = (
                    (attachment.content_type or "image/jpeg")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                img_bytes = await attachment.read()
                images.append((img_bytes, media_type))

            user_text = message.content.strip()
            async with message.channel.typing():
                response = await self._on_message(user_text, images=images)
                for chunk in _chunk(response, 1900):
                    await message.channel.send(chunk)
                if self._speak_in_voice and self._voice_client:
                    asyncio.create_task(self._play_tts(response))
        except Exception:
            logger.exception("Error handling image attachment")

    # ------------------------------------------------------------------
    # Manual join / leave
    # ------------------------------------------------------------------

    async def _handle_join_voice(self, message: discord.Message) -> None:
        target_channel = None
        for guild in self._client.guilds:
            member = guild.get_member(settings.discord_user_id)
            if member and member.voice and member.voice.channel:
                target_channel = member.voice.channel
                break

        if target_channel is None:
            await message.channel.send(
                "Join a voice channel first, then say \"join voice\"."
            )
            return

        await self._stop_voice_listening()
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect(force=True)

        try:
            self._voice_client = await target_channel.connect()
            self._speak_in_voice = True
            await self._start_voice_listening()
            logger.info("Manually joined voice channel: %s", target_channel.name)
        except Exception:
            logger.exception("Failed to join voice channel")
            await message.channel.send("Couldn't connect to the voice channel.")

    async def _handle_leave_voice(self, message: discord.Message) -> None:
        self._speak_in_voice = False
        await self._stop_voice_listening()
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect(force=True)
            self._voice_client = None
        await message.channel.send("Left the voice channel.")

    # ------------------------------------------------------------------
    # TTS playback
    # ------------------------------------------------------------------

    async def _play_tts(self, text: str) -> None:
        if not self._voice_client or not self._voice_client.is_connected():
            return

        audio_path = await synthesize(text)
        if audio_path is None:
            return

        try:
            ffmpeg_exe = get_ffmpeg_exe()
            source = discord.FFmpegPCMAudio(str(audio_path), executable=ffmpeg_exe)

            while self._voice_client.is_playing():
                await asyncio.sleep(0.1)

            self._voice_client.play(source)

            while self._voice_client.is_playing():
                await asyncio.sleep(0.1)
        except Exception:
            logger.exception("TTS playback failed")
        finally:
            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Proactive messaging
    # ------------------------------------------------------------------

    async def send_message(self, text: str) -> None:
        if self._text_channel is not None:
            for chunk in _chunk(text, 1900):
                await self._text_channel.send(chunk)
        elif self._dm_channel is not None:
            for chunk in _chunk(text, 1900):
                await self._dm_channel.send(chunk)
        else:
            logger.warning("No channel ready — cannot send proactive message")
            return
        if self._speak_in_voice and self._voice_client:
            asyncio.create_task(self._play_tts(text))

    async def is_reachable(self) -> bool:
        return self._dm_channel is not None or self._text_channel is not None

    async def start(self) -> None:
        await self._client.start(settings.discord_bot_token)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _is_voice_message(message: discord.Message) -> bool:
    try:
        return bool(message.flags.voice)
    except AttributeError:
        return False


def _chunk(text: str, size: int) -> list[str]:
    return [text[i: i + size] for i in range(0, len(text), size)]
