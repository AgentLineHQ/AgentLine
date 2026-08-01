"""
AgentLine — Configuration
Loads environment variables with validation via pydantic-settings.
Database URL is derived from SUPABASE_URL automatically.
"""

from pydantic_settings import BaseSettings
from pydantic import computed_field
from functools import lru_cache


class Settings(BaseSettings):
    # Supabase (Auth + Database)
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # SignalWire Configuration
    SIGNALWIRE_PROJECT_ID: str = ""
    SIGNALWIRE_TOKEN: str = ""
    SIGNALWIRE_SPACE_URL: str = ""

    # Voice Pipeline — LLM
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"  # Default to OpenAI, or set to Inception Labs

    # Voice Pipeline — STT (Deepgram)
    DEEPGRAM_API_KEY: str = ""

    # Voice Pipeline — TTS (Cartesia)
    CARTESIA_API_KEY: str = ""

    # App
    SECRET_KEY: str = "change-me-in-production"
    BASE_URL: str = "http://localhost:8000"
    WEBHOOK_SECRET_SALT: str = "change-me-in-production"

    # Database — override if needed, otherwise derived from Supabase URL
    DATABASE_URL: str = ""

    @property
    def base_url_clean(self) -> str:
        """BASE_URL with trailing slashes stripped to prevent double-slash URLs."""
        return self.BASE_URL.rstrip("/")

    @property
    def db_dsn(self) -> str:
        """Returns asyncpg-compatible DSN from DATABASE_URL."""
        if self.DATABASE_URL:
            # asyncpg requires postgresql:// instead of postgres://
            url = self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres://", "postgresql://")
            return url
        return "postgresql://agentline:secret@localhost:5432/agentline"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
