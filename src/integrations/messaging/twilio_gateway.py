"""
Twilio SMS gateway.

Flow:
  Your iPhone → SMS → Twilio → POST /webhooks/twilio (Railway)
      → agent.handle_message()
      → Twilio REST API → SMS reply to your phone

Setup:
  1. Sign up at twilio.com (free trial gives ~$15 credit)
  2. Buy a phone number (~$1/month) — save as TWILIO_PHONE_NUMBER
  3. Account SID + Auth Token from twilio.com/console → TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
  4. Phone number → Configure → Messaging → Webhook:
       https://{railway-url}/webhooks/twilio  (HTTP POST)
  5. Set TWILIO_USER_PHONE to your personal number so proactive messages work
"""

import asyncio
import logging

import httpx
from fastapi import FastAPI, Request, Response

from config import settings
from .base import MessagingGateway

logger = logging.getLogger(__name__)

# Twilio has a practical limit of ~1600 chars per message segment
SMS_CHUNK_SIZE = 1500


class TwilioGateway(MessagingGateway):
    """
    SMS gateway via Twilio.

    Receives messages via FastAPI webhook, sends replies via Twilio REST API.
    """

    def __init__(
        self,
        on_message,
        app: FastAPI | None = None,
    ) -> None:
        self._on_message = on_message
        # Pre-populate so proactive messages work on fresh start
        self._user_phone: str = settings.twilio_user_phone.strip()
        self._app = app
        self._register_routes()

    def _register_routes(self) -> None:
        if self._app is None:
            return

        @self._app.post("/webhooks/twilio")
        async def twilio_webhook(request: Request) -> Response:
            # Twilio sends form-encoded data
            try:
                form = await request.form()
            except Exception:
                return Response(
                    content="<?xml version='1.0'?><Response/>",
                    media_type="text/xml",
                    status_code=400,
                )

            body = (form.get("Body") or "").strip()
            from_number = (form.get("From") or "").strip()

            if not body:
                return Response(
                    content="<?xml version='1.0'?><Response/>",
                    media_type="text/xml",
                )

            # Track the user's number for replies
            if from_number:
                self._user_phone = from_number

            logger.info("SMS from %s: %s", from_number, body[:80])

            # Process async so webhook returns immediately (Twilio expects fast ACK)
            asyncio.create_task(self._handle_and_reply(body))

            # Empty TwiML response — we send the reply via REST API instead
            return Response(
                content="<?xml version='1.0'?><Response/>",
                media_type="text/xml",
            )

    async def _handle_and_reply(self, text: str) -> None:
        try:
            response = await self._on_message(text)
            await self.send_message(response)
        except Exception:
            logger.exception("Error handling Twilio SMS message")

    async def send_message(self, text: str) -> None:
        """Send SMS via Twilio REST API. Splits long messages into chunks."""
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            raise RuntimeError("Twilio credentials not configured")
        if not self._user_phone:
            raise RuntimeError("No user phone number — set TWILIO_USER_PHONE in env")
        if not settings.twilio_phone_number:
            raise RuntimeError("TWILIO_PHONE_NUMBER not configured")

        url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)

        # Split into chunks if the message is long
        chunks = [text[i:i + SMS_CHUNK_SIZE] for i in range(0, len(text), SMS_CHUNK_SIZE)]

        async with httpx.AsyncClient(timeout=15) as client:
            for chunk in chunks:
                r = await client.post(
                    url,
                    auth=auth,
                    data={
                        "From": settings.twilio_phone_number,
                        "To": self._user_phone,
                        "Body": chunk,
                    },
                )
                if r.status_code not in (200, 201):
                    raise RuntimeError(
                        f"Twilio API error: HTTP {r.status_code} — {r.text[:200]}"
                    )

        logger.info("Sent SMS reply (%d chars, %d chunk(s))", len(text), len(chunks))

    async def is_reachable(self) -> bool:
        return bool(
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and settings.twilio_phone_number
            and self._user_phone
        )

    async def start(self) -> None:
        """No-op: the FastAPI server is started externally in main.py."""
        pass
