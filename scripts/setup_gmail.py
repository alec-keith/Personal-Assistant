#!/usr/bin/env python3
"""
Gmail OAuth setup script.

Run this LOCALLY (not on Railway) for each Gmail account:
  python scripts/setup_gmail.py personal
  python scripts/setup_gmail.py business

This opens a browser, asks you to authorize, then saves a token file.
Upload the token file to Railway or copy it to the Railway volume.

Requirements:
  pip install google-auth-oauthlib google-api-python-client
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def setup_account(account_id: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    # Find account config
    account_cfg = next(
        (a for a in settings.gmail_accounts if a["id"] == account_id), None
    )
    if not account_cfg:
        print(f"Account '{account_id}' not found in GMAIL_ACCOUNTS setting.")
        print("Available accounts:", [a["id"] for a in settings.gmail_accounts])
        sys.exit(1)

    creds_path = Path(account_cfg["credentials_path"])
    token_path = Path(account_cfg["token_path"])

    if not creds_path.exists():
        print(f"\nCredentials file not found at: {creds_path}")
        print("\nTo get it:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create a project (or select existing)")
        print("  3. Enable 'Gmail API'")
        print("  4. Go to 'OAuth consent screen' → External → fill in app name")
        print("  5. Go to 'Credentials' → Create Credentials → OAuth client ID")
        print("  6. Application type: Desktop app")
        print("  7. Download JSON → rename to credentials.json")
        print(f"  8. Place it at: {creds_path}")
        sys.exit(1)

    print(f"\nAuthorizing Gmail account: {account_cfg.get('label', account_id)}")
    print("A browser window will open. Sign in and grant access.\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())

    print(f"\nToken saved to: {token_path}")
    print("\nNext steps for Railway:")
    print(f"  1. Copy this file to your Railway volume at the same path")
    print(f"  2. Or use: railway run python scripts/setup_gmail.py {account_id}")
    print(f"     (if Railway CLI is linked and volume is mounted)\n")

    # Quick verify
    svc = build("gmail", "v1", credentials=creds)
    profile = svc.users().getProfile(userId="me").execute()
    print(f"Authorized as: {profile.get('emailAddress')}")
    print("Setup complete!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/setup_gmail.py <account_id>")
        print("       python scripts/setup_gmail.py personal")
        print("       python scripts/setup_gmail.py business")
        sys.exit(1)

    setup_account(sys.argv[1])
