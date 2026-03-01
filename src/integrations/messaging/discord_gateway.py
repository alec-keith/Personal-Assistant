import asyncio
import logging
from typing import Callable, Awaitable

import discord
from discord.ext import commands

from config import settings
from .base import MessagingGateway

logger = logging.getLogger(__name__)


class DiscordGateway(MessagingGateway):
    """
    Discord DM-based messaging gateway.

    The bot only responds to DMs from your personal account (DISCORD_USER_ID).
    All other messages are ignored, keeping this truly personal.
    """

    def __init__(self, on_message: Callable[[str], Awaitable[str]]) -> None:
        self._on_message = on_message
        self._dm_channel: discord.DMChannel | None = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True

        self._client = discord.Client(intents=intents)
        self._register_events()

    def _register_events(self) -> None:
        client = self._client

        @client.event
        async def on_ready() -> None:
            logger.info("Discord gateway online as %s", client.user)
            # Pre-fetch the DM channel so we can send proactive messages
            user = await client.fetch_user(settings.discord_user_id)
            self._dm_channel = await user.create_dm()
            logger.info("DM channel ready with user %s", user.name)

        @client.event
        async def on_message(message: discord.Message) -> None:
            # Ignore messages not from you
            if message.author.id != settings.discord_user_id:
                return
            # Only handle DMs
            if not isinstance(message.channel, discord.DMChannel):
                return
            # Ignore the bot's own messages
            if message.author.bot:
                return

            user_text = message.content.strip()
            if not user_text:
                return

            logger.info("Received message: %s", user_text[:80])

            async with message.channel.typing():
                try:
                    response = await self._on_message(user_text)
                    # Discord has a 2000-char limit per message
                    for chunk in _chunk(response, 1900):
                        await message.channel.send(chunk)
                except Exception:
                    logger.exception("Error processing message")
                    await message.channel.send(
                        "Sorry, I ran into an error. Check the logs."
                    )

    async def send_message(self, text: str) -> None:
        """Proactively send a message to you."""
        if self._dm_channel is None:
            logger.warning("DM channel not ready yet — cannot send proactive message")
            return
        for chunk in _chunk(text, 1900):
            await self._dm_channel.send(chunk)

    async def start(self) -> None:
        await self._client.start(settings.discord_bot_token)


def _chunk(text: str, size: int) -> list[str]:
    """Split text into chunks that fit Discord's message limit."""
    return [text[i : i + size] for i in range(0, len(text), size)]
