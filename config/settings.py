from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
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

    # Discord
    discord_bot_token: str
    discord_user_id: int  # Your personal Discord user ID for DMs

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

    @property
    def use_icloud(self) -> bool:
        return bool(self.icloud_username and self.icloud_app_password)

    @property
    def use_google_calendar(self) -> bool:
        return bool(self.google_calendar_credentials_path)


settings = Settings()
