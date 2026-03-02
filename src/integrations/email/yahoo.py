"""
Yahoo Mail (and any IMAP provider) integration.

Uses standard IMAP over SSL. Yahoo requires an app password:
  login.yahoo.com → Account Security → Generate app password

Config per account:
  email: str            — Yahoo email address
  app_password: str     — App password (not account password)
  label: str            — Human-readable name (e.g. "Yahoo Personal")
  imap_host: str        — Default: imap.mail.yahoo.com
  smtp_host: str        — Default: smtp.mail.yahoo.com
"""

import asyncio
import email as email_lib
import imaplib
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

YAHOO_IMAP = "imap.mail.yahoo.com"
YAHOO_SMTP = "smtp.mail.yahoo.com"
YAHOO_SMTP_PORT = 587


class YahooAccount:
    def __init__(
        self,
        account_id: str,
        label: str,
        email_addr: str,
        app_password: str,
        imap_host: str = YAHOO_IMAP,
        smtp_host: str = YAHOO_SMTP,
    ) -> None:
        self.account_id = account_id
        self.label = label
        self._email = email_addr
        self._password = app_password
        self._imap_host = imap_host
        self._smtp_host = smtp_host

    def is_configured(self) -> bool:
        return bool(self._email and self._password)

    # ------------------------------------------------------------------
    # List / search (runs in executor — imaplib is sync)
    # ------------------------------------------------------------------

    async def list_messages(
        self,
        folder: str = "INBOX",
        unread_only: bool = False,
        count: int = 10,
    ) -> list[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._list_messages_sync, folder, unread_only, count
        )

    def _list_messages_sync(self, folder: str, unread_only: bool, count: int) -> list[dict]:
        with imaplib.IMAP4_SSL(self._imap_host) as imap:
            imap.login(self._email, self._password)
            imap.select(folder, readonly=True)

            criteria = "UNSEEN" if unread_only else "ALL"
            _, data = imap.search(None, criteria)
            ids = data[0].split()

            # Most recent first
            ids = ids[-count:][::-1]

            messages = []
            for msg_id in ids:
                try:
                    _, raw = imap.fetch(msg_id, "(RFC822.HEADER)")
                    parsed = email_lib.message_from_bytes(raw[0][1])
                    messages.append({
                        "id": msg_id.decode(),
                        "account_id": self.account_id,
                        "account_label": self.label,
                        "from": parsed.get("From", ""),
                        "to": parsed.get("To", ""),
                        "subject": parsed.get("Subject", ""),
                        "date": parsed.get("Date", ""),
                        "snippet": "",
                        "unread": unread_only,  # approximate
                    })
                except Exception as e:
                    logger.debug("Failed to parse message %s: %s", msg_id, e)

            return messages

    async def search(self, query: str, count: int = 10) -> list[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._search_sync, query, count)

    def _search_sync(self, query: str, count: int) -> list[dict]:
        with imaplib.IMAP4_SSL(self._imap_host) as imap:
            imap.login(self._email, self._password)
            imap.select("INBOX", readonly=True)

            # IMAP search supports subject/from/body
            _, data = imap.search(None, f'TEXT "{query}"')
            ids = data[0].split()
            ids = ids[-count:][::-1]

            messages = []
            for msg_id in ids:
                try:
                    _, raw = imap.fetch(msg_id, "(RFC822.HEADER)")
                    parsed = email_lib.message_from_bytes(raw[0][1])
                    messages.append({
                        "id": msg_id.decode(),
                        "account_id": self.account_id,
                        "account_label": self.label,
                        "from": parsed.get("From", ""),
                        "to": parsed.get("To", ""),
                        "subject": parsed.get("Subject", ""),
                        "date": parsed.get("Date", ""),
                        "snippet": "",
                        "unread": False,
                    })
                except Exception as e:
                    logger.debug("Failed to parse message %s: %s", msg_id, e)

            return messages

    async def get_message(self, message_id: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_message_sync, message_id)

    def _get_message_sync(self, message_id: str) -> dict:
        with imaplib.IMAP4_SSL(self._imap_host) as imap:
            imap.login(self._email, self._password)
            imap.select("INBOX", readonly=True)
            _, raw = imap.fetch(message_id.encode(), "(RFC822)")
            parsed = email_lib.message_from_bytes(raw[0][1])

            body = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
            else:
                body = parsed.get_payload(decode=True).decode("utf-8", errors="replace")

            return {
                "id": message_id,
                "account_id": self.account_id,
                "account_label": self.label,
                "from": parsed.get("From", ""),
                "to": parsed.get("To", ""),
                "subject": parsed.get("Subject", ""),
                "date": parsed.get("Date", ""),
                "body": body[:3000],
            }

    async def send(self, to: str, subject: str, body: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_sync, to, subject, body)

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = MIMEMultipart()
        msg["From"] = self._email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(self._smtp_host, YAHOO_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(self._email, self._password)
            server.sendmail(self._email, to, msg.as_string())
        logger.info("Sent Yahoo email from %s to %s", self.label, to)

    async def trash(self, message_id: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._trash_sync, message_id)

    def _trash_sync(self, message_id: str) -> None:
        with imaplib.IMAP4_SSL(self._imap_host) as imap:
            imap.login(self._email, self._password)
            imap.select("INBOX")
            imap.store(message_id.encode(), "+FLAGS", "\\Deleted")
            imap.expunge()
