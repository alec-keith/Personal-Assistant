import json
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Claude — model routing
    anthropic_api_key: str
    claude_model_complex: str = "claude-sonnet-4-6"          # everything except simple tool ops
    claude_model_simple: str = "claude-haiku-4-5-20251001"   # add/schedule/remind/list tasks

    # Discord (optional — used as fallback when BlueBubbles is unavailable)
    discord_bot_token: str = ""
    discord_user_id: int = 0
    discord_channel_id: int = 0  # server channel for proactive messages + @mention

    # BlueBubbles / iMessage (primary channel)
    bluebubbles_server_url: str = ""    # e.g. https://abc123.ngrok.io
    bluebubbles_password: str = ""      # BB server password
    bluebubbles_imessage_handle: str = ""  # your phone number or Apple ID email

    # Groq (Whisper transcription — free tier)
    groq_api_key: str = ""

    # Todoist
    todoist_api_token: str

    # iCloud CalDAV (Fantastical backend)
    icloud_username: str = ""
    icloud_app_password: str = ""

    # Google Calendar (alternative)
    google_calendar_credentials_path: str = ""
    google_calendar_id: str = "primary"

    # Memory (PostgreSQL — Railway auto-injects DATABASE_URL via Postgres plugin)
    database_url: str = ""

    # Voyage AI (semantic embeddings — optional, falls back to full-text search)
    voyage_api_key: str = ""

    # Tavily (web search + page fetch)
    tavily_api_key: str = ""

    # NewsAPI (news headlines — free 100 req/day at newsapi.org)
    newsapi_key: str = ""

    # ClickUp (task management)
    clickup_api_token: str = ""

    # Email accounts
    # Gmail: JSON list of {id, label, credentials_path, token_path}
    gmail_accounts_json: str = "[]"
    # Yahoo/IMAP: JSON list of {id, label, email, app_password}
    yahoo_accounts_json: str = "[]"

    @property
    def gmail_accounts(self) -> list[dict]:
        try:
            return json.loads(self.gmail_accounts_json)
        except Exception:
            return []

    @property
    def yahoo_accounts(self) -> list[dict]:
        try:
            return json.loads(self.yahoo_accounts_json)
        except Exception:
            return []

    # Default location for weather (no key needed — Open-Meteo is free)
    user_location: str = "New York"

    # Agent
    agent_name: str = "Atlas"
    agent_timezone: str = "America/New_York"

    # HTTP server port (used by BlueBubbles webhook receiver + health endpoint)
    port: int = 8080

    # ------------------------------------------------------------------
    # Computed helpers
    # ------------------------------------------------------------------

    @property
    def use_icloud(self) -> bool:
        return bool(self.icloud_username and self.icloud_app_password)

    @property
    def use_google_calendar(self) -> bool:
        return bool(self.google_calendar_credentials_path)

    @property
    def use_discord(self) -> bool:
        return bool(self.discord_bot_token and self.discord_user_id)

    @property
    def use_bluebubbles(self) -> bool:
        return bool(self.bluebubbles_server_url and self.bluebubbles_password)


settings = Settings()
