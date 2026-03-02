"""
EmailManager — aggregates all configured email accounts (Gmail + Yahoo).

Accounts are configured via settings:
  GMAIL_ACCOUNTS  — JSON list of Gmail account configs
  YAHOO_ACCOUNTS  — JSON list of Yahoo account configs

Each account has a unique `id` used to target specific accounts in tool calls.
"""

import logging
from typing import Union

from .gmail import GmailAccount
from .yahoo import YahooAccount

logger = logging.getLogger(__name__)

AnyAccount = Union[GmailAccount, YahooAccount]


class EmailManager:
    def __init__(self, accounts: list[AnyAccount]) -> None:
        self._accounts = {a.account_id: a for a in accounts}

    @classmethod
    def from_settings(cls) -> "EmailManager":
        from config import settings
        accounts: list[AnyAccount] = []

        for cfg in settings.gmail_accounts:
            try:
                acc = GmailAccount(
                    account_id=cfg["id"],
                    label=cfg.get("label", cfg["id"]),
                    credentials_path=cfg["credentials_path"],
                    token_path=cfg["token_path"],
                )
                if acc.is_configured():
                    accounts.append(acc)
                    logger.info("Gmail account loaded: %s", acc.label)
                else:
                    logger.warning("Gmail account %s missing credentials — skipping", cfg["id"])
            except Exception as e:
                logger.warning("Failed to load Gmail account %s: %s", cfg.get("id"), e)

        for cfg in settings.yahoo_accounts:
            try:
                acc = YahooAccount(
                    account_id=cfg["id"],
                    label=cfg.get("label", cfg["id"]),
                    email_addr=cfg["email"],
                    app_password=cfg["app_password"],
                )
                if acc.is_configured():
                    accounts.append(acc)
                    logger.info("Yahoo account loaded: %s", acc.label)
            except Exception as e:
                logger.warning("Failed to load Yahoo account %s: %s", cfg.get("id"), e)

        return cls(accounts)

    @property
    def available(self) -> bool:
        return bool(self._accounts)

    def account_list_str(self) -> str:
        if not self._accounts:
            return "No email accounts configured."
        return ", ".join(f"{a.label} [{a.account_id}]" for a in self._accounts.values())

    def _get_account(self, account_id: str | None) -> AnyAccount | None:
        if account_id:
            return self._accounts.get(account_id)
        # Default to first account
        return next(iter(self._accounts.values()), None)

    def _all_accounts(self) -> list[AnyAccount]:
        return list(self._accounts.values())

    # ------------------------------------------------------------------
    # Public interface (called from agent tool handlers)
    # ------------------------------------------------------------------

    async def list_emails(
        self,
        account_id: str | None = None,
        folder: str = "INBOX",
        unread_only: bool = False,
        count: int = 10,
    ) -> str:
        if not self._accounts:
            return "No email accounts configured."

        targets = [self._get_account(account_id)] if account_id else self._all_accounts()
        targets = [t for t in targets if t is not None]

        all_messages: list[dict] = []
        for acc in targets:
            try:
                msgs = await acc.list_messages(folder=folder, unread_only=unread_only, count=count)
                all_messages.extend(msgs)
            except Exception as e:
                logger.warning("Failed to list email from %s: %s", acc.label, e)

        if not all_messages:
            return "No emails found."

        return _format_email_list(all_messages[:count])

    async def search_emails(self, query: str, account_id: str | None = None, count: int = 10) -> str:
        if not self._accounts:
            return "No email accounts configured."

        targets = [self._get_account(account_id)] if account_id else self._all_accounts()
        targets = [t for t in targets if t is not None]

        all_messages: list[dict] = []
        for acc in targets:
            try:
                msgs = await acc.search(query=query, count=count)
                all_messages.extend(msgs)
            except Exception as e:
                logger.warning("Search failed for %s: %s", acc.label, e)

        if not all_messages:
            return f'No emails found matching "{query}".'

        return _format_email_list(all_messages[:count])

    async def get_email(self, message_id: str, account_id: str) -> str:
        acc = self._get_account(account_id)
        if not acc:
            return f"Account '{account_id}' not found. Available: {self.account_list_str()}"
        try:
            msg = await acc.get_message(message_id)
            return _format_email_full(msg)
        except Exception as e:
            return f"Failed to fetch email: {e}"

    async def send_email(self, to: str, subject: str, body: str, account_id: str | None = None) -> str:
        acc = self._get_account(account_id)
        if not acc:
            return f"No account found. Available: {self.account_list_str()}"
        try:
            await acc.send(to=to, subject=subject, body=body)
            return f"Email sent from {acc.label} to {to}."
        except Exception as e:
            return f"Failed to send email: {e}"

    async def delete_email(self, message_id: str, account_id: str) -> str:
        acc = self._get_account(account_id)
        if not acc:
            return f"Account '{account_id}' not found."
        try:
            await acc.trash(message_id)
            return f"Email {message_id} moved to trash."
        except Exception as e:
            return f"Failed to delete email: {e}"

    async def archive_email(self, message_id: str, account_id: str) -> str:
        if not isinstance(self._get_account(account_id), GmailAccount):
            return "Archive is only supported for Gmail accounts. Use delete for Yahoo."
        acc = self._get_account(account_id)
        try:
            await acc.archive(message_id)  # type: ignore[union-attr]
            return f"Email {message_id} archived."
        except Exception as e:
            return f"Failed to archive email: {e}"


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

def _format_email_list(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        acct = m.get("account_label", m.get("account_id", "?"))
        unread_flag = " [UNREAD]" if m.get("unread") else ""
        lines.append(
            f"[{m['id']}] ({acct}){unread_flag}\n"
            f"  From: {m.get('from', '?')}\n"
            f"  Subject: {m.get('subject', '(no subject)')}\n"
            f"  Date: {m.get('date', '?')}"
        )
        if m.get("snippet"):
            lines.append(f"  Preview: {m['snippet']}")
    return "\n\n".join(lines)


def _format_email_full(m: dict) -> str:
    acct = m.get("account_label", m.get("account_id", "?"))
    return (
        f"Account: {acct}\n"
        f"From: {m.get('from', '?')}\n"
        f"To: {m.get('to', '?')}\n"
        f"Subject: {m.get('subject', '(no subject)')}\n"
        f"Date: {m.get('date', '?')}\n"
        f"\n{m.get('body', '(no body)')}"
    )
