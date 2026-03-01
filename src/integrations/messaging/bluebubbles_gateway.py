"""
BlueBubbles iMessage gateway.

Flow:
  Your iPhone → iMessage → BlueBubbles Server (Mac)
      → POST /webhooks/bluebubbles (this server, on Railway)
      → agent.handle_message()
      → POST {BB_URL}/api/v1/message/text (back to Mac)
      → BlueBubbles → iMessage reply

Setup (on Mac):
  1. Download BlueBubbles Server from https://bluebubbles.app
  2. API & Webhooks tab → enable ngrok (free ngrok account required)
  3. Copy the ngrok URL → set BLUEBUBBLES_SERVER_URL in .env
  4. Add webhook → set to https://{your-railway-url}/webhooks/bluebubbles
  5. Set a server password → set BLUEBUBBLES_PASSWORD in .env
"""

import asyncio
import logging
from typing import Callable, Awaitable

import httpx
from fastapi import FastAPI, Request, Response
import uvicorn

from config import settings
from .base import MessagingGateway

logger = logging.getLogger(__name__)


class BlueBubblesGateway(MessagingGateway):
    """
    iMessage gateway via BlueBubbles Server on Mac.

    Runs a FastAPI HTTP server to receive webhooks from BlueBubbles.
    Sends replies back via the BlueBubbles REST API.
    """

    def __init__(
        self,
        on_message: Callable[[str], Awaitable[str]],
        app: FastAPI | None = None,
    ) -> None:
        self._on_message = on_message
        # Pre-populate from BLUEBUBBLES_IMESSAGE_HANDLE so proactive messages work on fresh start.
        # Format: "iMessage;-;+15551234567" or "iMessage;-;email@icloud.com"
        handle = settings.bluebubbles_imessage_handle.strip()
        self._active_chat_guid: str | None = (
            f"iMessage;-;{handle}" if handle else None
        )
        # Shared FastAPI app (so Discord health endpoint and BB webhook share one server)
        self._app = app
        self._register_routes()

    def _register_routes(self) -> None:
        app = self._app
        if app is None:
            return

        @app.post("/webhooks/bluebubbles")
        async def bluebubbles_webhook(request: Request) -> Response:
            try:
                payload = await request.json()
            except Exception:
                return Response(status_code=400)

            event_type = payload.get("type")
            data = payload.get("data", {})

            # Only handle incoming messages (not our own sent messages)
            if event_type != "new-message":
                return Response(status_code=200)
            if data.get("isFromMe", True):
                return Response(status_code=200)

            text = (data.get("text") or "").strip()
            if not text:
                return Response(status_code=200)

            # Extract chat GUID for routing replies
            chats = data.get("chats", [])
            if chats:
                self._active_chat_guid = chats[0].get("guid")

            handle = data.get("handle", {}).get("address", "unknown")
            logger.info("iMessage from %s: %s", handle, text[:80])

            # Process async so webhook returns immediately (BB expects fast ACK)
            asyncio.create_task(self._handle_and_reply(text))
            return Response(status_code=200)

    async def _handle_and_reply(self, text: str) -> None:
        try:
            response = await self._on_message(text)
            await self.send_message(response)
        except Exception:
            logger.exception("Error handling BlueBubbles message")

    async def send_message(self, text: str) -> None:
        """Send a message back via BlueBubbles REST API."""
        if not settings.bluebubbles_server_url:
            raise RuntimeError("BLUEBUBBLES_SERVER_URL not configured")
        if not self._active_chat_guid:
            raise RuntimeError("No active chat GUID — no message has been received yet")

        url = f"{settings.bluebubbles_server_url.rstrip('/')}/api/v1/message/text"
        params = {"guid": settings.bluebubbles_password}
        body = {
            "chatGuid": self._active_chat_guid,
            "text": text,
            "method": "private-api",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, params=params, json=body)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"BlueBubbles API error: HTTP {r.status_code} — {r.text[:200]}")

        logger.info("Sent iMessage reply (%d chars)", len(text))

    async def is_reachable(self) -> bool:
        """Check if the BlueBubbles server on the Mac is reachable."""
        if not settings.bluebubbles_server_url:
            return False
        try:
            url = f"{settings.bluebubbles_server_url.rstrip('/')}/api/v1/ping"
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(url, params={"guid": settings.bluebubbles_password})
            return r.status_code == 200
        except Exception:
            return False

    async def start(self) -> None:
        """No-op: the FastAPI server is started externally in main.py."""
        pass
