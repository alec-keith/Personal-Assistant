from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Claude
    anthropic_api_key: str
    claude_model: str = "claude-opus-4-6"

    # Discord (optional — used as fallback when BlueBubbles is unavailable)
    discord_bot_token: str = ""
    discord_user_id: int = 0

    # BlueBubbles / iMessage (primary channel)
    bluebubbles_server_url: str = ""    # e.g. https://abc123.ngrok.io
    bluebubbles_password: str = ""      # BB server password
    bluebubbles_imessage_handle: str = ""  # your phone number or Apple ID email

    # Todoist
    todoist_api_token: str

    # iCloud CalDAV (Fantastical backend)
    icloud_username: str = ""
    icloud_app_password: str = ""

    # Google Calendar (alternative)
    google_calendar_credentials_path: str = ""
    google_calendar_id: str = "primary"

    # Memory
    chroma_persist_dir: str = str(ROOT_DIR / "data" / "memory")

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
