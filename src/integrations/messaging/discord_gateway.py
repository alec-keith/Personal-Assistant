"""
Discord gateway — DM + voice support.

Text DMs: normal chat with Roman.
Voice message / audio attachments: transcribed via Groq Whisper, handled as text.
Voice channel: Roman auto-joins when you enter a voice channel, auto-leaves when you leave.
               Speaks all responses aloud via TTS while in the channel.
"""

import asyncio
import logging
from typing import Callable, Awaitable

import discord

from config import settings
from .base import MessagingGateway
from src.integrations.transcription import transcribe_bytes
from src.integrations.tts import synthesize, get_ffmpeg_exe

logger = logging.getLogger(__name__)

# Audio MIME types we'll attempt to transcribe
AUDIO_TYPES = {
    "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav",
    "audio/x-m4a", "audio/aiff", "audio/webm", "video/mp4",
}

# These still work as manual overrides, but auto-join/leave is the primary flow
JOIN_VOICE_CMDS = {"join voice", "join call", "/voice", "/join"}
LEAVE_VOICE_CMDS = {"leave voice", "leave call", "/leave", "/disconnect"}


class DiscordGateway(MessagingGateway):
    def __init__(self, on_message: Callable[[str], Awaitable[str]]) -> None:
        self._on_message = on_message
        self._dm_channel: discord.DMChannel | None = None
        self._text_channel: discord.TextChannel | None = None
        self._voice_client: discord.VoiceClient | None = None
        # Whether to also speak Roman's text responses in the voice channel
        self._speak_in_voice: bool = False

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
            # Only care about your own voice state changes
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
                target = after.channel
                # Disconnect from any previous channel first
                if self._voice_client and self._voice_client.is_connected():
                    await self._voice_client.disconnect(force=True)
                try:
                    self._voice_client = await target.connect()
                    self._speak_in_voice = True
                    logger.info("Auto-joined voice channel: %s", target.name)
                except Exception:
                    logger.exception("Failed to auto-join voice channel %s", target.name)

            elif left:
                self._speak_in_voice = False
                if self._voice_client and self._voice_client.is_connected():
                    await self._voice_client.disconnect(force=True)
                    self._voice_client = None
                logger.info("Auto-left voice channel")

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

            # ---- Voice channel commands ----
            cmd = message.content.strip().lower()

            if cmd in JOIN_VOICE_CMDS:
                await self._handle_join_voice(message)
                return

            if cmd in LEAVE_VOICE_CMDS:
                await self._handle_leave_voice(message)
                return

            # ---- Voice message or audio attachment ----
            if message.attachments:
                for attachment in message.attachments:
                    ct = (attachment.content_type or "").split(";")[0].strip().lower()
                    is_audio = ct in AUDIO_TYPES or _is_voice_message(message)
                    if is_audio:
                        asyncio.create_task(
                            self._handle_audio(message, attachment)
                        )
                        return

            # ---- Regular text message ----
            user_text = message.content.strip()
            if not user_text:
                return

            logger.info("Received message: %s", user_text[:80])
            async with message.channel.typing():
                try:
                    response = await self._on_message(user_text)
                    chunks = _chunk(response, 1900)
                    mention = f"<@{settings.discord_user_id}>"
                    await message.channel.send(f"{mention} {chunks[0]}")
                    for chunk in chunks[1:]:
                        await message.channel.send(chunk)
                    # Speak in voice channel if joined
                    if self._speak_in_voice and self._voice_client:
                        asyncio.create_task(self._play_tts(response))
                except Exception:
                    logger.exception("Error processing message")
                    await message.channel.send("Hit an error. Check the logs.")

    # ------------------------------------------------------------------
    # Audio attachment handler (voice memos)
    # ------------------------------------------------------------------

    async def _handle_audio(
        self, message: discord.Message, attachment: discord.Attachment
    ) -> None:
        try:
            await message.channel.send("🎙 Transcribing...")
            audio_bytes = await attachment.read()
            filename = attachment.filename or "audio.ogg"
            text = await transcribe_bytes(audio_bytes, filename)

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
                # Speak in voice channel if joined
                if self._speak_in_voice and self._voice_client:
                    asyncio.create_task(self._play_tts(response))
        except Exception:
            logger.exception("Error handling audio attachment")

    # ------------------------------------------------------------------
    # Voice channel — join / leave / speak
    # ------------------------------------------------------------------

    async def _handle_join_voice(self, message: discord.Message) -> None:
        target_channel: discord.VoiceChannel | None = None
        for guild in self._client.guilds:
            member = guild.get_member(settings.discord_user_id)
            if member and member.voice and member.voice.channel:
                target_channel = member.voice.channel
                break

        if target_channel is None:
            await message.channel.send(
                "Join a voice channel first, then DM me \"join voice\"."
            )
            return

        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect(force=True)

        try:
            self._voice_client = await target_channel.connect()
            self._speak_in_voice = True
            logger.info("Manually joined voice channel: %s", target_channel.name)
        except Exception:
            logger.exception("Failed to join voice channel")
            await message.channel.send("Couldn't connect to the voice channel.")

    async def _handle_leave_voice(self, message: discord.Message) -> None:
        self._speak_in_voice = False
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect(force=True)
            self._voice_client = None
        await message.channel.send("Left the voice channel.")
        logger.info("Left voice channel")

    async def _play_tts(self, text: str) -> None:
        """Synthesize and play text in the voice channel."""
        if not self._voice_client or not self._voice_client.is_connected():
            return

        # Keep spoken responses concise
        spoken = text[:400] if len(text) > 400 else text

        aiff_path = await synthesize(spoken)
        if aiff_path is None:
            return

        try:
            ffmpeg_exe = get_ffmpeg_exe()
            source = discord.FFmpegPCMAudio(str(aiff_path), executable=ffmpeg_exe)

            # Wait for any current playback to finish
            while self._voice_client.is_playing():
                await asyncio.sleep(0.1)

            self._voice_client.play(source)

            while self._voice_client.is_playing():
                await asyncio.sleep(0.1)
        except Exception:
            logger.exception("TTS playback failed")
        finally:
            try:
                aiff_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Proactive messaging
    # ------------------------------------------------------------------

    async def send_message(self, text: str) -> None:
        # Prefer server channel (triggers @mention notification) over DM
        if self._text_channel is not None:
            mention = f"<@{settings.discord_user_id}>"
            chunks = _chunk(text, 1900)
            await self._text_channel.send(f"{mention} {chunks[0]}")
            for chunk in chunks[1:]:
                await self._text_channel.send(chunk)
        elif self._dm_channel is not None:
            for chunk in _chunk(text, 1900):
                await self._dm_channel.send(chunk)
        else:
            logger.warning("No channel ready — cannot send proactive message")
            return
        # Speak proactive messages in voice too if joined
        if self._speak_in_voice and self._voice_client:
            asyncio.create_task(self._play_tts(text))

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
    return [text[i : i + size] for i in range(0, len(text), size)]
