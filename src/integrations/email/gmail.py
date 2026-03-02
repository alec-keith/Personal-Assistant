"""
Gmail integration via Google Gmail REST API.

Each account needs:
  - credentials.json  (OAuth client secret — download from Google Cloud Console)
  - token.json        (generated on first run via setup script, then persisted)

Scopes: read mail, send mail, modify labels (archive/trash).
"""

import asyncio
import base64
import email as email_lib
import functools
import logging
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailAccount:
    def __init__(self, account_id: str, label: str, credentials_path: str, token_path: str) -> None:
        self.account_id = account_id
        self.label = label
        self._credentials_path = Path(credentials_path)
        self._token_path = Path(token_path)
        self._service: Any = None  # googleapiclient resource

    def _build_service(self) -> Any:
        """Build Gmail API service. Raises if credentials not found."""
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self._token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self._credentials_path.exists():
                    raise FileNotFoundError(
                        f"Gmail credentials not found at {self._credentials_path}. "
                        f"Run: python scripts/setup_gmail.py {self.account_id}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)

            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    async def _svc(self) -> Any:
        """Get or build the service (cached, built in thread pool)."""
        if self._service is None:
            loop = asyncio.get_event_loop()
            self._service = await loop.run_in_executor(None, self._build_service)
        return self._service

    def is_configured(self) -> bool:
        return self._credentials_path.exists() or self._token_path.exists()

    # ------------------------------------------------------------------
    # List / search
    # ------------------------------------------------------------------

    async def list_messages(
        self,
        folder: str = "INBOX",
        unread_only: bool = False,
        count: int = 10,
    ) -> list[dict]:
        query_parts = [f"in:{folder.lower()}"]
        if unread_only:
            query_parts.append("is:unread")
        query = " ".join(query_parts)
        return await self._search(query, count)

    async def search(self, query: str, count: int = 10) -> list[dict]:
        return await self._search(query, count)

    async def _search(self, query: str, count: int) -> list[dict]:
        svc = await self._svc()
        loop = asyncio.get_event_loop()

        def _fetch() -> list[dict]:
            result = svc.users().messages().list(
                userId="me", q=query, maxResults=count
            ).execute()
            messages = result.get("messages", [])
            summaries = []
            for msg in messages:
                try:
                    full = svc.users().messages().get(
                        userId="me", id=msg["id"], format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    ).execute()
                    summaries.append(_parse_message_summary(full, self.account_id, self.label))
                except Exception as e:
                    logger.debug("Failed to fetch message %s: %s", msg["id"], e)
            return summaries

        return await loop.run_in_executor(None, _fetch)

    async def get_message(self, message_id: str) -> dict:
        svc = await self._svc()
        loop = asyncio.get_event_loop()

        def _fetch() -> dict:
            full = svc.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
            return _parse_message_full(full, self.account_id, self.label)

        return await loop.run_in_executor(None, _fetch)

    async def send(self, to: str, subject: str, body: str) -> str:
        svc = await self._svc()
        loop = asyncio.get_event_loop()

        def _send() -> str:
            msg = MIMEText(body)
            msg["to"] = to
            msg["subject"] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            result = svc.users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()
            return result["id"]

        msg_id = await loop.run_in_executor(None, _send)
        logger.info("Sent Gmail from %s to %s (id=%s)", self.label, to, msg_id)
        return msg_id

    async def archive(self, message_id: str) -> None:
        svc = await self._svc()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: svc.users().messages().modify(
                userId="me", id=message_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()
        )

    async def trash(self, message_id: str) -> None:
        svc = await self._svc()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: svc.users().messages().trash(userId="me", id=message_id).execute()
        )


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_message_summary(msg: dict, account_id: str, label: str) -> dict:
    headers = msg.get("payload", {}).get("headers", [])
    snippet = msg.get("snippet", "")
    labels = msg.get("labelIds", [])
    return {
        "id": msg["id"],
        "account_id": account_id,
        "account_label": label,
        "from": _get_header(headers, "From"),
        "to": _get_header(headers, "To"),
        "subject": _get_header(headers, "Subject"),
        "date": _get_header(headers, "Date"),
        "snippet": snippet[:200],
        "unread": "UNREAD" in labels,
    }


def _parse_message_full(msg: dict, account_id: str, label: str) -> dict:
    summary = _parse_message_summary(msg, account_id, label)
    body = _extract_body(msg.get("payload", {}))
    summary["body"] = body[:3000]  # truncate very long emails
    return summary


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body_data = (payload.get("body") or {}).get("data", "")

    if body_data and mime_type == "text/plain":
        return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    # Fallback: try HTML part
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html":
            data = (part.get("body") or {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                # Strip tags crudely
                import re
                return re.sub(r"<[^>]+>", "", html)[:3000]

    return ""
